CREATE TABLE rubrics (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text NOT NULL UNIQUE,
    role_description text NOT NULL,
    competencies     jsonb NOT NULL,
    embedding        vector(1024) NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
