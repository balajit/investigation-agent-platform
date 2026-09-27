# src/investigation_agent_platform/infrastructure/evidence/schema/sql.py
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp
from pydantic import BaseModel


class TableColumn(BaseModel):
    name: str
    data_type: str
    is_primary_key: bool = False


class TableSchema(BaseModel):
    name: str
    columns: list[TableColumn]
    foreign_keys: list[dict[str, str]]


class SchemaParser:
    """Parses static SQL DDL scripts into structured schema metadata."""

    def parse_ddl_file(self, ddl_path: Path) -> list[TableSchema]:
        sql_content = ddl_path.read_text(encoding="utf-8")
        tables: list[TableSchema] = []

        for statement in sqlglot.parse(sql_content):
            if isinstance(statement, exp.Create):
                table_name = statement.this.this.this.output_name
                columns = []
                fks = []

                # Parse column definitions and foreign key constraints
                for schema_item in statement.this.expressions:
                    if isinstance(schema_item, exp.ColumnDef):
                        col_name = schema_item.this.output_name
                        col_kind = schema_item.kind
                        col_type = col_kind.sql() if col_kind is not None else "UNKNOWN"
                        is_pk = any(
                            isinstance(c, exp.PrimaryKeyColumnConstraint)
                            for c in schema_item.constraints
                        )
                        columns.append(
                            TableColumn(name=col_name, data_type=col_type, is_primary_key=is_pk)
                        )
                    elif isinstance(schema_item, exp.ForeignKey):
                        # Extract table relationship mappings
                        ref_schema = schema_item.find(exp.Schema)
                        ref_this = ref_schema.this if ref_schema is not None else None
                        if ref_this is None:
                            continue
                        ref_table = ref_this.output_name
                        fks.append({"ref_table": ref_table})

                tables.append(TableSchema(name=table_name, columns=columns, foreign_keys=fks))
        return tables
