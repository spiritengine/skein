"""
SKEIN data models using Pydantic.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field


# Roster Models

AgentType = Literal["claude-code", "patbot", "horizon", "human", "system"]


class AgentRegistration(BaseModel):
    agent_id: str
    name: Optional[str] = None
    agent_type: Optional[AgentType] = None
    description: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentInfo(BaseModel):
    agent_id: str
    name: Optional[str] = None
    agent_type: Optional[AgentType] = None
    description: Optional[str] = None
    registered_at: datetime
    capabilities: List[str]
    status: str = "active"
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Site Models

class SiteCreate(BaseModel):
    site_id: str
    purpose: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Site(BaseModel):
    site_id: str
    created_at: datetime
    created_by: str
    purpose: str
    status: str = "active"
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Folio Models

FolioType = Literal["issue", "friction", "brief", "summary", "finding", "notion", "tender", "playbook", "mantle", "plan"]


class FolioCreate(BaseModel):
    type: FolioType
    site_id: str
    title: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    assigned_to: Optional[str] = None
    target_agent: Optional[str] = None  # For briefs
    successor_name: Optional[str] = None  # Suggested name for successor
    omlet: Optional[str] = None  # Reference to agent execution (strand_id/agent_id/turn-N)


class Folio(BaseModel):
    folio_id: str
    type: FolioType
    site_id: str
    created_at: datetime
    created_by: str
    title: str
    content: str
    status: str = "open"
    assigned_to: Optional[str] = None
    target_agent: Optional[str] = None
    successor_name: Optional[str] = None
    omlet: Optional[str] = None  # Reference to agent execution (strand_id/agent_id/turn-N)
    archived: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)
    acknowledged_at: Optional[datetime] = None


class FolioUpdate(BaseModel):
    """Model for updating a folio's mutable fields."""
    title: Optional[str] = None
    content: Optional[str] = None
    status: Optional[str] = None
    assigned_to: Optional[str] = None
    archived: Optional[bool] = None


# Thread Models

ThreadType = Literal["message", "mention", "reference", "assignment", "succession", "reply", "tag", "status"]


class ThreadCreate(BaseModel):
    from_id: str  # Any resource ID (agent, folio, etc)
    to_id: str    # Any resource ID
    type: ThreadType
    content: Optional[str] = None
    weaver: Optional[str] = None  # Agent who created this connection


class Thread(BaseModel):
    thread_id: str
    from_id: str
    to_id: str
    type: ThreadType
    content: Optional[str] = None
    weaver: Optional[str] = None  # Agent who created this connection
    created_at: datetime
    read_at: Optional[datetime] = None


# Log Models

class LogEntry(BaseModel):
    stream_id: str
    level: str = "INFO"
    message: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LogBatch(BaseModel):
    stream_id: str
    source: str
    lines: List[LogEntry]


class LogLine(BaseModel):
    id: int
    stream_id: str
    timestamp: datetime
    level: str
    source: str
    message: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Screenshot Models

class ScreenshotCreate(BaseModel):
    screenshot_data: str  # base64 PNG
    strand_id: str
    turn_number: Optional[int] = None
    label: str = "auto"


class Screenshot(BaseModel):
    screenshot_id: str
    strand_id: str
    timestamp: datetime
    turn_number: Optional[int] = None
    label: str
    file_path: str
    file_size: int
    metadata: Dict[str, Any] = Field(default_factory=dict)
