CREATE TABLE evaluations (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rubric_id               uuid NULL REFERENCES rubrics (id) ON DELETE SET NULL,
    request                 jsonb NOT NULL,
    result                  jsonb NOT NULL,
    calibration_example_ids jsonb NOT NULL DEFAULT '[]',
    approved                boolean NOT NULL DEFAULT false,
    approved_scores         jsonb NULL,
    approved_recommendation text NULL,
    embedding               vector(1024) NULL,  -- NULL when embeddings are not configured
    created_at              timestamptz NOT NULL DEFAULT now(),
    approved_at             timestamptz NULL
);

CREATE INDEX evaluations_embedding_idx ON evaluations USING hnsw (embedding vector_cosine_ops);
CREATE INDEX evaluations_approved_rubric_idx ON evaluations (rubric_id) WHERE approved;
