from elasticsearch import Elasticsearch
import json

es = Elasticsearch(
['https://xcos-elk.eaas.comcast.net/es/'],
    basic_auth=('xcos_api_service_account', 'xc0s@p1'),
    verify_certs=False,
    request_timeout=1200
)

indices = es.cat.indices(format="json")

for index in indices:
    print(index["index"], index.get("docs.count"))

mappings = es.indices.get_mapping(index="*")

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