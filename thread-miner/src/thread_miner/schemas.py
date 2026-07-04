"""All pydantic models, exactly as specified (spec section 13.2)."""

from pydantic import BaseModel, Field
from typing import Literal


class CommentLabel(BaseModel):
    comment_id: str            # t1_xxx, must exist in the chunk
    sentiment: Literal["positive", "negative", "neutral", "mixed"]


class ExtractedItem(BaseModel):
    comment_id: str
    text: str = Field(max_length=500)


class ChunkExtraction(BaseModel):
    labels: list[CommentLabel]
    questions: list[ExtractedItem]
    pain_points: list[ExtractedItem]
    objections: list[ExtractedItem]
    vocabulary: list[str] = Field(max_length=30)


class ThemeCluster(BaseModel):
    name: str
    description: str
    supporting_comment_ids: list[str]


class QuestionCluster(BaseModel):
    canonical_question: str
    member_comment_ids: list[str]


class ContentAngle(BaseModel):
    title: str
    rationale: str
    supporting_comment_ids: list[str]


class ThreadSynthesis(BaseModel):
    summary: str
    themes: list[ThemeCluster]
    question_clusters: list[QuestionCluster]
    contrarian_takes: list[ExtractedItem]
    audience_vocabulary: list[str]
    content_angles: list[ContentAngle]


class CrossThreadTheme(BaseModel):
    name: str
    description: str
    post_fullnames: list[str]


class Rollup(BaseModel):
    executive_summary: str
    cross_thread_themes: list[CrossThreadTheme]
    content_opportunity_brief: str
