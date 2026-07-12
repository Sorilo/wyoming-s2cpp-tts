"""Phase 9B / 9.5 speech domain package.

Exports core domain objects: SpeechMetadata, SpeechRequest, SpeechState,
ScheduledSpeech, SpeechScheduler, error types, PhraseAccumulator,
AudioEnvelope, EnvelopeError, and StreamingCoordinator.
"""

from app.speech.models import (
    SpeechMetadata,
    SpeechRequest,
    SpeechState,
    ScheduledSpeech,
)
from app.speech.phrases import PhraseAccumulator
from app.speech.envelope import AudioEnvelope, EnvelopeError
from app.speech.scheduler import (
    SpeechScheduler,
    QueueFullError,
    QueueTimeoutError,
)

__all__ = [
    "SpeechMetadata",
    "SpeechRequest",
    "SpeechState",
    "ScheduledSpeech",
    "SpeechScheduler",
    "QueueFullError",
    "QueueTimeoutError",
    "PhraseAccumulator",
    "AudioEnvelope",
    "EnvelopeError",
]
