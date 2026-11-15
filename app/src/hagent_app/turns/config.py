"""The ``turns`` section of ``options.json``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TurnsConfig:
    """Limits for prompts, history and compaction (design section 13.1).

    - ``tool_result_max_chars``: the model receives at most this many
      characters of one tool result. History keeps the full result.
    - ``recent_records``: the number of latest records of a source that go
      into the prompt in full.
    - ``summary_batch``: the summary model folds older records into the
      running summary when more than ``recent_records + summary_batch``
      records are not in the summary.
    - ``record_max_chars``: the maximum length of one record in the prompt.
    - ``summary_max_chars``: the maximum length of a running summary.
    - ``compaction_target_tokens``: inside a turn, the compaction strategy
      clears old tool results when the context is larger than this.
    - ``compaction_min_clear_tokens``: the minimum saving of one clear. A
      clear changes the start of the conversation, so a small saving costs
      more in cache misses than it saves (design section 13.2).
    - ``compaction_keep_pairs``: the latest tool results that stay.
    - ``index_max_entries``: the maximum entries of one store index.
    - ``main_file_max_chars``: the size limit of an injected main file.
    - ``main_file_warn_fraction``: at this part of the limit, the runtime
      asks the agent to shorten the main file.
    """

    tool_result_max_chars: int = 8_000
    recent_records: int = 5
    summary_batch: int = 5
    record_max_chars: int = 1_000
    summary_max_chars: int = 4_000
    compaction_target_tokens: int = 60_000
    compaction_min_clear_tokens: int = 4_000
    compaction_keep_pairs: int = 2
    index_max_entries: int = 50
    main_file_max_chars: int = 16_000
    main_file_warn_fraction: float = 0.8
