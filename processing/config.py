"""Processing-worker settings, loaded from the environment / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ProcessingSettings(BaseSettings):
    """Worker cadence and retry policy, read from PROCESSING_* variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PROCESSING_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # How long a worker waits for a NOTIFY before running another
    # discover-and-drain pass; also the retry/discovery latency ceiling.
    poll_interval_seconds: float = 5.0
    discovery_batch: int = 100

    # Stamped onto jobs at enqueue (the table default is the backstop).
    max_attempts: int = 5
    retry_backoff_base_seconds: float = 2.0
    retry_backoff_cap_seconds: float = 300.0
    # Requeue delay when a backing service is down; its own recovery loop
    # (retry -> exit -> restart -> cold load) needs a few minutes.
    unavailable_retry_seconds: float = 30.0

    # Supervisor: SIGTERM -> SIGKILL window and child-restart backoff.
    shutdown_grace_seconds: float = 20.0
    restart_backoff_base_seconds: float = 1.0
    restart_backoff_cap_seconds: float = 30.0
    restart_reset_seconds: float = 60.0

    # Speech detection (the VAD tier) — see processing/vad.py for the span
    # assembly these tune.
    vad_backend: str = "silero"  # or "energy": deterministic RMS threshold
    # Deliberately low: spans are a coarse, high-recall activity gate that
    # feeds the diarize tier's block assembly — no longer the ASR gate. On a
    # glasses mic silero scores far (non-user) speakers low, so precision
    # here would silence one side of every conversation.
    vad_threshold: float = 0.15
    vad_min_speech_ms: int = 250
    # Wide enough that ordinary conversational pauses don't split a span;
    # only a real lull does. Long pauses SHOULD split: silence wastes GPU
    # time and invites ASR hallucination.
    vad_merge_gap_ms: int = 2500
    vad_pad_ms: int = 200
    # The transcription model's input window: Canary/Whisper-family models
    # are trained on <=~40s clips, so spans must stay inside that envelope.
    # Forced cuts land on the quietest frame near the cap (processing/vad.py).
    vad_max_span_ms: int = 30_000
    vad_energy_threshold: int = 500  # int16 RMS amplitude, energy backend only

    # Transcription tier. The base URL speaks the standard transcriptions API
    # ("openai" protocol: POST {base}/audio/transcriptions) or a cog wrapper
    # ("cog": POST {base}/predictions) — swapping backends is env-only.
    transcriber_base_url: str = "http://127.0.0.1:8033/v1"
    transcriber_model: str = "nvidia/parakeet-tdt-0.6b-v3"
    transcriber_protocol: str = "openai"  # or "cog"
    transcriber_timeout_seconds: float = 600.0

    # Diarization tier. Speech spans re-merge into blocks (contiguous speech,
    # gap-joined and capped) because diarization label consistency needs long
    # context — see docs/processing-pipelines.md.
    diarizer_base_url: str = "http://127.0.0.1:8034/v1"
    diarizer_timeout_seconds: float = 900.0  # a 30-min block is minutes of GPU
    # 60 s: also the conversation tier's pause tolerance, since a
    # conversation never spans a block (see conversation_gap_ms).
    diarize_block_merge_gap_ms: int = 60_000
    diarize_max_block_ms: int = 1_800_000
    diarize_min_turn_ms: int = 300
    # Utterances: same-speaker turns merged into the ASR units the transcribe
    # tier consumes. The cap is the transcription model's input window, same
    # rationale as vad_max_span_ms.
    diarize_utterance_merge_gap_ms: int = 1_500
    diarize_max_utterance_ms: int = 30_000
    # A same-speaker merge may not span another speaker's speech beyond this:
    # ASR transcribes the whole rendered span, so an interjection inside a
    # merge gap lands in the wrong speaker's transcript. Small enough to
    # tolerate grunt-level backchannels, which shouldn't split a sentence.
    diarize_merge_crosstalk_max_ms: int = 300

    # Purity audit: the diarizer can wrongly put several people under one local
    # label; per-turn embeddings (clean audio only) are locally clustered per
    # label and well-separated groups become sub-labels (SPEAKER_XX.0, .1…).
    # The split threshold is looser than the corpus cluster_distance because
    # within-block recording conditions are uniform — it should only fire on
    # clearly-bimodal labels (community-1: in-block cross-voice pairs sit at
    # ~0.78 median, 5th pct 0.48; observed genuine splits fire around ~0.73).
    # Carried over to DiariZen unchanged (same embedder, same geometry), but
    # measured under community-1's segmentation — wants a re-sweep.
    diarize_split_distance: float = 0.7
    # A sub-cluster must carry this much clean speech to become a sub-label;
    # smaller groups fold into the nearest surviving one. Calibrated on a
    # real 13-speaker glasses block: at 2s virtually every label sheds a
    # marginal ~2s group (turn-level vectors are noisy), at 5s only strong
    # second voices split — and same-voice over-splits self-heal anyway when
    # the corpus HAC re-merges prints closer than cluster_distance.
    diarize_split_min_clean_ms: int = 5_000
    # Turn embeddings from less clean audio than this don't vote in the
    # audit (mirrors the service's EMBED_MIN_CLEAN_MS floor).
    diarize_turn_min_clean_ms: int = 1_000

    # Audio-tag tier: sound-event classes + CLAP embeddings per ~10s window.
    # The base URL speaks the audio_tagger contract (POST {base}/audio/analyze
    # multipart -> per-window labels + embedding); swapping backends is
    # env-only. Rendered spans are large so a session is a handful of uploads,
    # overlapping by window-minus-hop to keep the service's hop grid
    # continuous across seams (window/hop here mirror the service's defaults).
    classifier_base_url: str = "http://127.0.0.1:8035/v1"
    classifier_timeout_seconds: float = 600.0
    audio_tag_span_ms: int = 120_000
    audio_tag_window_ms: int = 10_000
    audio_tag_hop_ms: int = 5_000
    # Per-window floor: labels below it aren't worth a metadata row. Session
    # tags additionally need `threshold` held for `min_consecutive` windows
    # in a row — transient false positives rarely persist across two.
    audio_tag_window_min_score: float = 0.15
    audio_tag_threshold: float = 0.3
    audio_tag_min_consecutive: int = 2
    audio_tag_top_k: int = 15

    # Vision sidecar (SigLIP 2 image/text embeddings) and the keyframe tier.
    # Candidates are sampled at candidate_fps; a gated frame becomes a
    # keyframe when its cosine distance to every one of the last
    # keyframe_memory keyframes reaches keyframe_novelty. Calibrated on a
    # real phone clip (2026-11-15): one continuous shot drifts 0.02-0.14 from
    # its first frame, a different room with the same person sits ~0.15, and
    # unrelated photos ~0.22; 0.10 picked wide shot, close-up and lean-back
    # out of 11.5s. SigLIP distances are compressed: re-sweep before raising.
    vision_base_url: str = "http://127.0.0.1:8036/v1"
    vision_timeout_seconds: float = 300.0
    keyframe_candidate_fps: float = 2.0
    keyframe_novelty: float = 0.10
    keyframe_memory: int = 8
    keyframe_settle_ms: int = 1_000
    keyframe_min_gap_ms: int = 1_500
    keyframe_heartbeat_ms: int = 30_000
    keyframe_max_per_minute: int = 12
    keyframe_blur_percentile: float = 25.0

    # Sound-span tier: the tagger's windows merged into few long single-class
    # spans. Hysteresis, not one threshold — sigmoid scores wobble, and a class
    # sitting near a single cut-off shatters into fragments. A span opens at
    # `enter` and holds while `sustain` does. `sustain` must stay above
    # audio_tag_window_min_score (0.15): below that floor a label is simply
    # absent, so the service's floor would be making the call instead of us.
    sound_span_enter_score: float = 0.40
    sound_span_sustain_score: float = 0.30
    # Dropout tolerance as ms of audio no member window covered — not a window
    # count, so it survives missing windows and hop changes.
    sound_span_bridge_gap_ms: int = 5_000
    # A one-window span is already ~10s wide, so the evidence floor is stated
    # in windows rather than milliseconds.
    sound_span_min_windows: int = 3
    # Classes rank by total covered time, not peak. Lower than audio_tag_top_k
    # because each class becomes a timeline lane.
    sound_span_top_k: int = 8
    # Row guard: create_many binds 9 params/row against Postgres' 65535 ceiling
    # and does not chunk.
    sound_span_max_spans: int = 2_000

    # Conversation tier: utterances grouped into conversations by the silence
    # between one utterance's end and the next's start, then split where the
    # participants change. A conversation never spans a diarization block, so
    # its labels stay consistent and the effective pause tolerance is
    # min(conversation_gap_ms, diarize_block_merge_gap_ms). A lone utterance
    # never forms a conversation.
    conversation_gap_ms: int = 60_000
    conversation_min_turns: int = 2
    # Speaker-shift split: compare the non-user speaker sets of the trailing
    # and leading `churn_window` turns; split where they are disjoint and each
    # side holds at least `churn_min_turns` non-user turns (one interjection
    # is not a new conversation). The user is every label in the block that
    # resolves to the speaker cluster named `conversation_user_name`; with no
    # such label (unset, or not yet clustered) the block's dominant label.
    conversation_churn_window: int = 4
    conversation_churn_min_turns: int = 2
    conversation_user_name: str | None = None

    # Speaker clustering tier: batch agglomerative clustering (average
    # linkage, cosine) over per-(block, speaker) embeddings; merging stops at
    # this distance. Embeddings from under min_talk_ms of speech are skipped
    # as unreliable voice-prints. Both are env DEFAULTS — a cluster_params
    # row overrides them per user. Calibrated on real glasses sessions
    # (pyannote community-1, 2026-08-12 sweep): cross-voice pairs in one
    # block sit at 0.78 median / 0.68 25th pct, and both the same-block
    # false-merge count and the largest cluster's size accelerate sharply
    # past 0.65 — the window is narrower than 3.1's (same ≲0.6 vs ~0.9).
    # Unchanged across the DiariZen switch (same embedder); DiariZen's own AHC
    # cuts at 0.7, so re-sweep once there is turn-level ground truth.
    cluster_distance: float = 0.65
    cluster_min_talk_ms: int = 3_000


@lru_cache(maxsize=1)
def get_settings() -> ProcessingSettings:
    return ProcessingSettings()
