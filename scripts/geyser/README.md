# scripts/geyser/

The Yellowstone gRPC (Geyser) protobuf definitions and the Python stubs generated from them.
`03_listen_solana_grpc.py` and `06_listen_relay_payouts_grpc.py` import the stubs; nothing else
here is run.

## Where the protos come from

`proto/geyser.proto` and `proto/solana-storage.proto` are vendored from
[`yellowstone-grpc-proto/proto/`](https://github.com/rpcpool/yellowstone-grpc/tree/master/yellowstone-grpc-proto/proto)
in [rpcpool/yellowstone-grpc](https://github.com/rpcpool/yellowstone-grpc).

That subpackage is licensed **Apache-2.0** — its `LICENSE_APACHE2`, copied here as `LICENSE`. The
repository root carries AGPL-3.0, which covers the Geyser plugin itself rather than these
interface definitions; the `yellowstone-grpc-proto` crate declares `license = "Apache-2.0"`.

The copy is pinned and older than upstream. Since it was taken, upstream has added the
`SubscribeDeshred`, `SubscribeGossip` and `SubscribeReplayInfo` RPCs, prefixed the `SlotStatus`
values with `SLOT_`, and added `from_slot` to the subscribe request. The listeners here use
`Subscribe` and nothing else, so the copy stays as it is — updating it means regenerating the
stubs and re-reading both listeners against the new field names.

## Regenerating the stubs

`generated/` was produced with grpcio-tools 1.71.0 and protobuf 6.31.1:

```bash
uv run --with grpcio-tools python -m grpc_tools.protoc \
  -Iscripts/geyser/proto \
  --python_out=scripts/geyser/generated \
  --grpc_python_out=scripts/geyser/generated \
  scripts/geyser/proto/geyser.proto scripts/geyser/proto/solana-storage.proto
```

Two imports then have to be package-qualified by hand, because protoc emits them flat and the
stubs are imported as `geyser.generated.*`:

| File | protoc emits | it must read |
|---|---|---|
| `generated/geyser_pb2_grpc.py` | `import geyser_pb2 as geyser__pb2` | `import geyser.generated.geyser_pb2 as geyser__pb2` |
| `generated/geyser_pb2.py` | `from solana_storage_pb2 import *` | `from geyser.generated.solana_storage_pb2 import *` |

A newer grpcio-tools also rewrites the version guard and the descriptor bytes, so a regeneration
is never a small diff. Ruff skips `generated/` — `extend-exclude` in `pyproject.toml`.
