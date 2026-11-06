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
# The guide is a reading argument, not another abstract or a bag of metrics.
READING_GUIDE_SCHEMA = object_schema(
    {
        "problem": PARAGRAPH,
        "insight": PARAGRAPH,
        "mechanism": array_schema(object_schema({"title": TEXT, "explanation": PARAGRAPH})),
        "findings": array_schema(
            object_schema({"title": TEXT, "result": PARAGRAPH, "boundary": PARAGRAPH})
        ),
        "takeaway": PARAGRAPH,
    }
)
SECTION_PARAGRAPH = object_schema({**PARAGRAPH["properties"], "heading": TEXT})
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
        "reading_guide": READING_GUIDE_SCHEMA,
        "sections": array_schema(
            object_schema(
                {
                    "id": {"type": "string", "enum": list(SECTION_IDS)},
                    "title": TEXT,
                    "paragraphs": array_schema(SECTION_PARAGRAPH),
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
                    "headline": TEXT,
                    "importance": {"type": "string", "enum": ["core", "supporting"]},
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

NOTE_EXPANSION_SCHEMA = object_schema(
    {
        "sections": array_schema(
            object_schema(
                {
                    "id": {"type": "string", "enum": list(SECTION_IDS)},
                    "paragraphs": array_schema(SECTION_PARAGRAPH),
                }
            )
        )
    }
)
