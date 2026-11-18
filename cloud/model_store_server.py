"""Cloud model store: serves model files over plain HTTP so <mlModel> can point at them with `mmu`.

Registering the model by URL (mmu) instead of embedding the bytes (mmd) avoids two limits found in
pre-verification: Mobius4's default 1 MB request body, and tinyIoT's 64 KB payload when the edge
would fetch a large resource through the MN. The edge AE checks the file against the SHA-256 that
publish_model.py records in the <mlModel> labels, so the URL alone is not trusted.
"""
import argparse
import functools
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--dir", default=os.path.join(HERE, "model_store"))
    a = ap.parse_args()
    handler = functools.partial(SimpleHTTPRequestHandler, directory=a.dir)
    print(f"model store: serving {a.dir} on http://{a.host}:{a.port}/", flush=True)
    ThreadingHTTPServer((a.host, a.port), handler).serve_forever()


if __name__ == "__main__":
    main()
