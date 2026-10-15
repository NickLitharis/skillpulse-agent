"""SQLAlchemy ORM models (SPEC section 8: stored fields)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all SkillPulse tables."""


class Thread(Base):
    """One monthly 'Ask HN: Who is hiring?' thread."""

    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    month: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    descendant_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    posts: Mapped[list[Post]] = relationship(back_populates="thread")

    __table_args__ = (Index("ix_threads_month", "month"),)


class Post(Base):
    """One top-level comment in a hiring thread, i.e. one job post.

    One post per company per month is the trend signal, so duplicates are only
    defined by the HN item id (SPEC section 8.4).
    """

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    thread_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    month: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    text_raw: Mapped[str] = mapped_column(Text, nullable=False)
    text_clean: Mapped[str] = mapped_column(Text, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_dead: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    thread: Mapped[Thread] = relationship(back_populates="posts")

    __table_args__ = (
        Index("ix_posts_month", "month"),
        Index("ix_posts_thread_id", "thread_id"),
    )


class FetchRun(Base):
    """One fetch run, used for resumability and failure marking (SPEC section 8.5)."""

    __tablename__ = "fetch_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("threads.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    posts_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    posts_upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_item_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('running', 'ok', 'failed')", name="ck_fetch_runs_status"),
        Index("ix_fetch_runs_thread_id", "thread_id"),
    )


class Skill(Base):
    """A skill from the fixed taxonomy (SPEC section 8, skills.yaml)."""

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    aliases: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("name", name="uq_skills_name"),)


class PostExtractionRow(Base):
    """One extractor's structured result for one post (SPEC section 8.2.3).

    The extractor and taxonomy version are part of the uniqueness rule, so re-running a
    new taxonomy never mixes with results produced by an older one.
    """

    __tablename__ = "post_extractions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    extractor: Mapped[str] = mapped_column(String(16), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    company: Mapped[str | None] = mapped_column(Text, nullable=True)
    role_family: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seniority: Mapped[str | None] = mapped_column(String(32), nullable=True)
    remote: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    countries: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    unknown_terms: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "post_id", "extractor", "taxonomy_version", name="uq_post_extractions_identity"
        ),
        CheckConstraint(
            "extractor IN ('baseline', 'llm') AND taxonomy_version <> ''",
            name="ck_post_extractions_identity",
        ),
        Index("ix_post_extractions_post_id", "post_id"),
        Index("ix_post_extractions_query", "taxonomy_version", "extractor"),
    )


class PostSkillRow(Base):
    """One extracted skill for one post, from one extractor (SPEC section 8.2)."""

    __tablename__ = "post_skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[str] = mapped_column(String(64), ForeignKey("skills.id"), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extractor: Mapped[str] = mapped_column(String(16), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "post_id", "skill_id", "extractor", "taxonomy_version",
            name="uq_post_skills_identity",
        ),
        Index("ix_post_skills_post_id", "post_id"),
        Index("ix_post_skills_skill_id", "skill_id"),
    )
