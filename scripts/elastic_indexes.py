"""List Elasticsearch indices + mappings (operator introspection, not platform-wired).

All connection details come from the environment — no hosts, users, or
passwords live in this file. Local dev against the compose stack needs only
IAP_ELASTICSEARCH_URL (defaults to loopback :9200); auth is basic-auth user
+ password when the cluster requires it. TLS verification stays on unless
IAP_ELASTIC_INSECURE=true is set explicitly (dev clusters with self-signed
certs only — never production).
"""

import json
import os
import sys

from elasticsearch import Elasticsearch

url = os.environ.get("IAP_ELASTICSEARCH_URL", "http://localhost:9200")
user = os.environ.get("IAP_ELASTICSEARCH_USER", "")
password = os.environ.get("IAP_ELASTICSEARCH_PASSWORD", "")
insecure = os.environ.get("IAP_ELASTIC_INSECURE", "false").lower() == "true"

kwargs: dict = {"request_timeout": 1200}
if user or password:
    if not (user and password):
        sys.exit("ERROR: set both IAP_ELASTICSEARCH_USER and IAP_ELASTICSEARCH_PASSWORD (or neither).")
    kwargs["basic_auth"] = (user, password)
if insecure:
    kwargs["verify_certs"] = False

es = Elasticsearch([url], **kwargs)

indices = es.cat.indices(format="json")

for index in indices:
    print(index["index"], index.get("docs.count"))

mappings = es.indices.get_mapping(index="*")

for index_name, index_data in mappings.items():
    print(f"\n{'=' * 100}")
    print(f"INDEX: {index_name}")
    print(f"{'=' * 100}")

    properties = index_data.get("mappings", {}).get("properties", {})

    print(json.dumps(properties, indent=2))
