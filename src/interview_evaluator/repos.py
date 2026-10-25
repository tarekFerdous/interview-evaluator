"""Repositories over Postgres. Plain SQL with psycopg 3; vectors via the pgvector adapter."""

from uuid import UUID

from pgvector import Vector
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import Rubric, RubricIn


class RubricRepo:
    COLUMNS = "id, name, role_description, competencies, created_at, updated_at"

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def _one(self, sql: str, params: tuple) -> Rubric | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(sql, params).fetchone()
        return Rubric(**row) if row else None

    def create(self, rubric: RubricIn, embedding: list[float]) -> Rubric:
        """Raises psycopg.errors.UniqueViolation on a duplicate name."""
        return self._one(
            f"INSERT INTO rubrics (name, role_description, competencies, embedding)"
            f" VALUES (%s, %s, %s, %s) RETURNING {self.COLUMNS}",
            (rubric.name, rubric.role_description, Jsonb(rubric.competencies), Vector(embedding)),
        )

    def get(self, rubric_id: UUID) -> Rubric | None:
        return self._one(f"SELECT {self.COLUMNS} FROM rubrics WHERE id = %s", (rubric_id,))

    def all(self) -> list[Rubric]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(f"SELECT {self.COLUMNS} FROM rubrics ORDER BY name").fetchall()
        return [Rubric(**r) for r in rows]

    def update(self, rubric_id: UUID, rubric: RubricIn, embedding: list[float]) -> Rubric | None:
        return self._one(
            f"UPDATE rubrics SET name = %s, role_description = %s, competencies = %s,"
            f" embedding = %s, updated_at = now() WHERE id = %s RETURNING {self.COLUMNS}",
            (rubric.name, rubric.role_description, Jsonb(rubric.competencies),
             Vector(embedding), rubric_id),
        )

    def delete(self, rubric_id: UUID) -> bool:
        return self.conn.execute("DELETE FROM rubrics WHERE id = %s", (rubric_id,)).rowcount > 0

    def nearest(self, embedding: list[float], limit: int) -> list[tuple[Rubric, float]]:
        """Rubrics by cosine similarity to `embedding`, most similar first."""
        with self.conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                f"SELECT {self.COLUMNS}, 1 - (embedding <=> %s) AS similarity"
                f" FROM rubrics ORDER BY embedding <=> %s LIMIT %s",
                (Vector(embedding), Vector(embedding), limit),
            ).fetchall()
        return [(Rubric(**{k: v for k, v in r.items() if k != "similarity"}), float(r["similarity"]))
                for r in rows]


def rubric_embedding_text(rubric: RubricIn) -> str:
    return f"{rubric.name}\n\n{rubric.role_description}\n\nCompetencies:\n" + "\n".join(
        f"- {c}" for c in rubric.competencies
    )
