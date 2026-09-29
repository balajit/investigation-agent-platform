from elasticsearch import Elasticsearch
import os
import json

url = os.environ.get("ES_URL", "")
user = os.environ.get("ES_USER", "")
password = os.environ.get("ES_PASSWORD", "")

print(f"url: {url}, user: {user}, password: {password}")


es = Elasticsearch(
[url],
    basic_auth=(user, password),
    verify_certs=False,
    request_timeout=1200
)

indices = es.cat.indices(format="json")

for index in indices:
    print(index["index"], index.get("docs.count"))

mappings = es.indices.get_mapping(index="logz-*")

for index_name, index_data in mappings.items():
    print(f"\n{'=' * 100}")
    print(f"INDEX: {index_name}")
    print(f"{'=' * 100}")

    properties = (
        index_data
        .get("mappings", {})
        .get("properties", {})
    )

    print(json.dumps(properties, indent=2))