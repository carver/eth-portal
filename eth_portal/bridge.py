from contextlib import ExitStack, contextmanager
from typing import Iterable, Tuple

from eth.db.trie import make_trie_root_and_nodes
from eth_hash.auto import keccak
from eth_utils import ValidationError, encode_hex
import rlp

from eth_portal.trin import launch_trin
from eth_portal.web3_decoding import block_fields_to_header, receipt_fields_to_receipt
from eth_portal.web3_encoding import (
    header_content_key,
    receipt_content_key,
    receipt_content_value,
)


class PortalInserter:
    """
    Track a group of Portal nodes, and simplify pushing content to them.

    Eventually, it will intelligently choose which nodes to broadcast content
    to. At documentation time, it naively pushes all content to all supplied nodes.
    """

    def __init__(self, web3_links):
        """
        Create an instance, with web3 links to the launched Portal nodes.
        """
        self._web3_links = web3_links

    def push_history(self, content_key: bytes, content_value: bytes):
        """
        Push the given Portal History content out to the group of portal clients.
        """
        content_key_hex = encode_hex(content_key)
        content_value_hex = encode_hex(content_value)

        print(
            "Propagate new history content with key, value:",
            content_key_hex,
            ",",
            content_value_hex,
        )

        # For now, just push to all inserting clients. When running more, be a
        #   bit smarter about selecting inserters closer to the content key
        for w3 in self._web3_links:
            w3.provider.make_request(
                "portal_historyStore", [content_key_hex, content_value_hex]
            )


def handle_new_header(
    w3, portal_inserter: PortalInserter, header_hash: bytes, chain_id=1
):
    """
    Handle header hash notifications by posting all new data to Portal History Network.

    This data to be propagated will at least include header, block bodies
    (uncles & transactions), and receipts. At documentation time, only headers
    are propagated.

    :param w3: web3 access to core Ethereum content
    :param portal_inserter: a class responsible for pushing content keys and
        values into the network via a group of running portal clients
    :param header_hash: the new header hash that we were notified exists on the network
    :param chain_id: Ethereum network Chain ID that this header exists on
    """
    block_fields = propagate_header(w3, portal_inserter, chain_id, header_hash)
    # TODO propagate bodies & receipts
    propagate_receipts(w3, portal_inserter, chain_id, block_fields)


def propagate_header(
    w3, portal_inserter: PortalInserter, chain_id: int, header_hash: bytes
):
    """
    React to new header hash notification by posting header to Portal History Network.

    :param w3: web3 access to core Ethereum content
    :param portal_inserter: a class responsible for pushing content keys and
        values into the network via a group of running portal clients
    :param chain_id: Ethereum network Chain ID that this header exists on
    :param header_hash: the new header hash that we were notified exists on the network

    :return: the web3 block fields for the given header hash
    """
    # Retrieve data to post to network
    block_fields = w3.eth.get_block(header_hash, full_transactions=False)

    # Encode data for posting
    content_key, content_value = block_fields_to_content(block_fields, chain_id)

    # Post data to trin nodes
    portal_inserter.push_history(content_key, content_value)

    return block_fields


def block_fields_to_content(block_fields, chain_id) -> Tuple[bytes, bytes]:
    """
    Convert a web3 block into a Portal History Network content key and value.

    A web3 block is the result of a w3.eth.get_block() request. A content key and
    value are the byte-strings specified by the Portal Network Spec.

    :return: (content_key, content_value)

    :raise ValidationError: if the rlp-encoded header does not match the header
        hash in `block_fields`
    """
    header = block_fields_to_header(block_fields)
    header_rlp = rlp.encode(header)

    if keccak(header_rlp) != block_fields.hash:
        raise ValidationError(
            f"Could not correctly encode header fields {block_fields} to {header!r}"
        )

    if header.hash != block_fields.hash:
        raise ValidationError(
            "py-evm generated a different hash than we did manually with"
            f" header fields {block_fields} to {header!r}"
        )

    content_key = header_content_key(block_fields.hash, chain_id)
    return content_key, header_rlp


def propagate_receipts(
    w3, portal_inserter: PortalInserter, chain_id: int, block_fields: bytes
):
    """
    React to new header hash notification by posting receipts to Portal History Network.

    :param w3: web3 access to core Ethereum content
    :param portal_inserter: a class responsible for pushing content keys and
        values into the network via a group of running portal clients
    :param chain_id: Ethereum network Chain ID that this header exists on
    :param block_fields: the web3 block fields for header to retrieve receipts from
    """
    # Retrieve data to post to network
    print("Collecting receipts to propagate...")
    web3_receipts = [
        w3.eth.wait_for_transaction_receipt(txn_hash, poll_latency=2)
        for txn_hash in block_fields.transactions
    ]
    print("Collected receipts")

    # Encode data for posting
    content_key, content_value = encode_receipts_content(
        web3_receipts,
        chain_id,
        block_fields.hash,
        block_fields.number,
        block_fields.receiptsRoot,
    )

    # Post data to trin nodes
    portal_inserter.push_history(content_key, content_value)


def encode_receipts_content(
    web3_receipts,
    chain_id: int,
    header_hash: bytes,
    block_number: int,
    receipt_root: hash,
) -> Tuple[bytes, bytes]:
    """
    Generate a Portal History Network content key and value, from a list of web3 receipts.

    :return: (content_key, content_value)

    :raise ValidationError: if the encoded receipts do not match the header's `receipt_root`
    """
    # Convert web3 receipts to py-evm receipts
    receipts = [
        receipt_fields_to_receipt(receipt, block_number) for receipt in web3_receipts
    ]

    # Validate against the receipt root
    calculated_root, _ = make_trie_root_and_nodes(receipts)
    if calculated_root != receipt_root:
        raise ValidationError(
            f"Could not correctly encode receipts for header {header_hash.hex()}"
        )

    content_key = receipt_content_key(header_hash, chain_id)
    content_value = receipt_content_value(receipts)
    return content_key, content_value


@contextmanager
def launch_trin_inserters(keys: Iterable[bytes]):
    """
    For each key supplied, launch an instance of trin, then yield an object for propagation.

    When this context manager exits, all the trin instances will also exit.

    Yields a :class:`PortalInserter` instance, to push data to the whole group
    of launched trin nodes.

    :param keys: list of private keys to launch each trin instance.
    """
    with ExitStack() as stack:
        web3_links = [
            stack.enter_context(launch_trin(key, 9000 + idx))
            for idx, key in enumerate(keys)
        ]
        yield PortalInserter(web3_links)
