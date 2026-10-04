from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (JSON, DateTime, ForeignKey, Index, Integer, MetaData, String,
                        Text, UniqueConstraint, Uuid, create_engine)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import settings


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid4())


J = JSON().with_variant(JSONB(), 'postgresql')


class Base(DeclarativeBase):
    metadata = MetaData(schema='app')
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Task(Base):
    __tablename__ = 'tasks'
    source_task_id: Mapped[str] = mapped_column(Text, unique=True)
    source_record: Mapped[dict] = mapped_column(J)
    source_sha256: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[str] = mapped_column(Text)


class Candidate(Base):
    __tablename__ = 'candidates'
    __table_args__ = (UniqueConstraint('task_id', 'code_sha256'),)
    task_id: Mapped[str] = mapped_column(ForeignKey('app.tasks.id'))
    code: Mapped[str] = mapped_column(Text)
    code_sha256: Mapped[str] = mapped_column(String(64))
    private_label: Mapped[str] = mapped_column(Text)


class Run(Base):
    __tablename__ = 'runs'
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    creation_request_sha256: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(Text)
    config_snapshot: Mapped[dict] = mapped_column(J)
    status: Mapped[str] = mapped_column(Text, default='queued')
    error: Mapped[dict | None] = mapped_column(J, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index('ix_runs_created', 'created_at', 'id'),)


class Call(Base):
    __tablename__ = 'agent_calls'
    __table_args__ = (UniqueConstraint('run_id', 'role', 'call_seq'), Index('ix_call_history', 'run_id', 'role', 'call_seq'))
    run_id: Mapped[str] = mapped_column(ForeignKey('app.runs.id'))
    task_id: Mapped[str] = mapped_column(ForeignKey('app.tasks.id'))
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey('app.candidates.id'))
    role: Mapped[str] = mapped_column(Text)
    call_seq: Mapped[int] = mapped_column(Integer)
    round_index: Mapped[int] = mapped_column(Integer)
    user_prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default='queued')
    error: Mapped[dict | None] = mapped_column(J, nullable=True)
    selected_response_id: Mapped[str | None] = mapped_column(ForeignKey('app.responses.id', use_alter=True, name='fk_selected_response'))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Snapshot(Base):
    __tablename__ = 'input_snapshots'
    __table_args__ = (UniqueConstraint('call_id', 'response_version'),)
    call_id: Mapped[str] = mapped_column(ForeignKey('app.agent_calls.id'))
    response_version: Mapped[int] = mapped_column(Integer)
    role_prompt: Mapped[str] = mapped_column(Text)
    task_input: Mapped[dict] = mapped_column(J)
    ordered_history: Mapped[list] = mapped_column(J)
    parent_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey('app.input_snapshots.id'))
    reset_reason: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))


class Response(Base):
    __tablename__ = 'responses'
    __table_args__ = (UniqueConstraint('call_id', 'response_version'),)
    call_id: Mapped[str] = mapped_column(ForeignKey('app.agent_calls.id'))
    response_version: Mapped[int] = mapped_column(Integer)
    raw_text: Mapped[str] = mapped_column(Text)
    parsed_output: Mapped[dict | None] = mapped_column(J, nullable=True)
    format_error: Mapped[str | None] = mapped_column(Text)
    response_sha256: Mapped[str] = mapped_column(String(64))


class Evaluation(Base):
    __tablename__ = 'evaluations'
    __table_args__ = (UniqueConstraint('response_id', 'phase', 'evaluation_version'),)
    response_id: Mapped[str] = mapped_column(ForeignKey('app.responses.id'))
    phase: Mapped[str] = mapped_column(Text)
    evaluation_version: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default='pending')
    lexical_result: Mapped[dict | None] = mapped_column(J, nullable=True)
    judge_result: Mapped[dict | None] = mapped_column(J, nullable=True)
    evidence_validation: Mapped[dict | None] = mapped_column(J, nullable=True)
    execution_evidence: Mapped[dict | None] = mapped_column(J, nullable=True)
    aggregate_verdict: Mapped[str | None] = mapped_column(Text)
    metric_snapshot: Mapped[dict | None] = mapped_column(J, nullable=True)


class ModelRequest(Base):
    __tablename__ = 'model_requests'
    __table_args__ = (UniqueConstraint('call_id', 'operation_key', 'attempt_number'),)
    call_id: Mapped[str] = mapped_column(ForeignKey('app.agent_calls.id'))
    input_snapshot_id: Mapped[str] = mapped_column(ForeignKey('app.input_snapshots.id'))
    evaluation_id: Mapped[str | None] = mapped_column(ForeignKey('app.evaluations.id'))
    operation_key: Mapped[str] = mapped_column(Text)
    attempt_number: Mapped[int] = mapped_column(Integer)
    request_payload: Mapped[dict] = mapped_column(J)
    response_payload: Mapped[dict | None] = mapped_column(J, nullable=True)
    usage: Mapped[dict | None] = mapped_column(J, nullable=True)
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[dict | None] = mapped_column(J, nullable=True)


class ToolExecution(Base):
    __tablename__ = 'tool_executions'
    __table_args__ = (UniqueConstraint('call_id', 'operation_key'),)
    call_id: Mapped[str] = mapped_column(ForeignKey('app.agent_calls.id'))
    operation_key: Mapped[str] = mapped_column(Text)
    arguments: Mapped[dict] = mapped_column(J)
    result: Mapped[dict | None] = mapped_column(J, nullable=True)
    stdout: Mapped[str | None] = mapped_column(Text)
    stderr: Mapped[str | None] = mapped_column(Text)
    junit_xml: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)


class Log(Base):
    __tablename__ = 'execution_logs'
    __table_args__ = (UniqueConstraint('call_id', 'event_seq'),)
    call_id: Mapped[str] = mapped_column(ForeignKey('app.agent_calls.id'))
    event_seq: Mapped[int] = mapped_column(Integer)
    node_name: Mapped[str] = mapped_column(Text)
    layer: Mapped[str | None] = mapped_column(Text)
    operation_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(J, default=dict)


class Job(Base):
    __tablename__ = 'jobs'
    __table_args__ = (Index('ix_job_claim', 'status', 'lease_expires_at'),)
    run_id: Mapped[str] = mapped_column(ForeignKey('app.runs.id'))
    job_key: Mapped[str] = mapped_column(Text, unique=True)
    job_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(J)
    status: Mapped[str] = mapped_column(Text, default='queued')
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict | None] = mapped_column(J, nullable=True)


engine = create_engine(settings.database_url, pool_pre_ping=True)
Session = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def transaction():
    with Session.begin() as session:
        yield session


def record(row):
    return {col.name: getattr(row, col.name) for col in row.__table__.columns}
