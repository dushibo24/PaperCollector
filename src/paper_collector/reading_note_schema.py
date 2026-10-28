def object_schema(properties):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def array_schema(items):
    return {"type": "array", "items": items}


TEXT = {"type": "string"}
PAGE = {"type": "integer", "minimum": 1}
PARAGRAPH = object_schema(
    {
        "text": TEXT,
        "pages": array_schema(PAGE),
        "kind": {"type": "string", "enum": ["claim", "analysis"]},
    }
)
SECTION_IDS = (
    "overview",
    "writing",
    "metadata",
    "story",
    "technical",
    "evaluation-protocol",
    "experiments",
    "source-discrepancies",
)
PLAN_SCHEMA = object_schema(
    {
        "artifacts": array_schema(
            object_schema(
                {
                    "label": TEXT,
                    "page": PAGE,
                    "bbox": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "role": {"type": "string", "enum": ["method", "data", "result", "analysis"]},
                    "caption": TEXT,
                }
            )
        ),
        "coverage_notes": TEXT,
        "no_artifacts_reason": TEXT,
    }
)
NOTE_SCHEMA = object_schema(
    {
        "paper_id": TEXT,
        "title_zh": TEXT,
        "subtitle": TEXT,
        "brief": PARAGRAPH,
        "sections": array_schema(
            object_schema(
                {
                    "id": {"type": "string", "enum": list(SECTION_IDS)},
                    "title": TEXT,
                    "paragraphs": array_schema(PARAGRAPH),
                }
            )
        ),
        "artifacts": array_schema(
            object_schema(
                {
                    "label": TEXT,
                    "page": PAGE,
                    "section": {
                        "type": "string",
                        "enum": ["technical", "evaluation-protocol", "experiments"],
                    },
                    "crop_verified": {"type": "boolean"},
                    "paper_claim": PARAGRAPH,
                    "analysis": PARAGRAPH,
                    "axes": PARAGRAPH,
                    "interpretation": PARAGRAPH,
                    "limit": PARAGRAPH,
                }
            )
        ),
        "data_record": object_schema(
            {
                "json": TEXT,
                "reconstructed": {"type": "boolean"},
                "pages": array_schema(PAGE),
                "fields": array_schema(object_schema({"name": TEXT, "description": PARAGRAPH})),
            }
        ),
        "evidence_synthesis": array_schema(
            object_schema(
                {name: PARAGRAPH for name in ("claim", "evidence", "key_values", "boundary")}
            )
        ),
        "discrepancies": array_schema(
            object_schema({"item": TEXT, "claim": PARAGRAPH, "verification": PARAGRAPH})
        ),
    }
)

TEXT_REPAIR_SCHEMA = object_schema(
    {"edits": array_schema(object_schema({"path": TEXT, "original": TEXT, "replacement": TEXT}))}
)
