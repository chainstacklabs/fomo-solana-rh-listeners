"""How a Yellowstone gRPC subscription is built, and what gets it past the door.

Two listeners open a Geyser stream: `03` on FOMO's co-signer, `06` on Relay's payout
solver. The channel credentials and the subscription request are the same either way and
differ only in the account watched, so both are values here, the way `block_subscription()`
in shared/solana.py is a value for the two WebSocket listeners.

Reading the stream stays in each listener, where what to do with an update is decided.
Nothing here makes a request: it builds the credentials and the request, and opening the
channel is the caller's.

Reference: docs/04-listening.md (Solana).
"""

import os

import grpc
from geyser.generated import geyser_pb2
from shared import env

# Both Geyser listeners read at `processed`, one commitment ahead of what the WebSocket
# pair sees, which is most of what the add-on buys. A payout read this early can still be
# rolled back with its slot; the fill it names is on the other chain either way.
COMMITMENT = geyser_pb2.CommitmentLevel.PROCESSED


def credentials():
    """Channel credentials carrying the token in whichever header the endpoint wants.

    Providers split on that header: `x-token` at most of them, `authorization` where the
    endpoint sits behind basic auth. `GEYSER_AUTH_TYPE` picks, and x-token is the default
    because it is what the majority serve.
    """
    token = env.endpoint("GEYSER_API_TOKEN")
    header = "x-token" if (os.environ.get("GEYSER_AUTH_TYPE") or "x-token").lower() == "x-token" else "authorization"
    call = grpc.metadata_call_credentials(lambda _, callback: callback(((header, token),), None))
    return grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), call)


def transaction_subscription(account):
    """The `Subscribe` request for every succeeding transaction that mentions `account`.

    `account_include` matches an account anywhere in the transaction, signer or not, which
    is the same handle `mentionsAccountOrProgram` gives the WebSocket listeners. Failures
    are dropped by the server rather than read and discarded here.

    The filter is named for the shape of the thing, not for what is being watched: the
    name comes back on each update as an echo and no listener reads it.
    """
    request = geyser_pb2.SubscribeRequest()
    request.transactions["watched"].account_include.append(account)
    request.transactions["watched"].failed = False
    request.commitment = COMMITMENT
    return request
