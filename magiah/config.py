# -*- coding: utf-8 -*-
"""Tunable thresholds for the detection pipeline."""
from dataclasses import dataclass, asdict, fields


@dataclass
class Config:
    rare_max: int = 2        # a word is "suspect" if it occurs <= rare_max times
    common_min: int = 30     # minimum corpus frequency for a correction candidate
    part_min: int = 100      # minimum frequency for each part of a split
    join_min: int = 100      # minimum frequency of the joined word (extra-space)
    ed1_ratio: int = 50      # min freq ratio correction/word for edit-distance-1
    max_part: int = 15       # maximum length of a split part
    max_parts: int = 3       # maximum number of parts in a split
    exp_prefilter: float = 5e-4   # expected-count prefilter for split candidates
    split_obs_min: int = 3   # min adjacent observations to confirm a split
    split_obs_min_short: int = 20  # ... when the shortest part has 2 letters
    split_alts_3: int = 3    # 3-part segmentations verified per word (best
                             # by part frequency), besides every 2-part one
    split_override_ratio: float = 10.0  # a split displaces an edit-1 style
                             # correction only when seen >= split_obs_min
                             # times AND this many times above chance
    struct_max: int = 10     # structural rules (final letter mid-word) apply
                             # up to this frequency, beyond rare_max; more
                             # frequent forms are established spellings
    nonfinal_max: int = 20   # non-final letter at word end: up to this freq
    foreign_ratio: float = 0.35  # skip lines with this share of uncommon words
                                 # (Judeo-Arabic / badly garbled passages)
    names_sugg: int = 0      # a rare word up to 2 words after a title is a
                             # name and skipped; with the name list
                             # (names.pkl, built with the lexicon) it is
                             # still reported when the corpus never had it
                             # in a name position and its suggestion is a
                             # name seen there this often. Only ever adds
                             # findings. 0 (or less): off
    ocr_pair_min: int = 8    # confirmed substitutions of one letter pair in a
                             # single book to establish an OCR profile
    book_spelling_min: int = 5  # a book that swaps one SOUND-alike (not
                             # look-alike) letter pair (א/ע, ס/ש, כ/ק, ט/ת)
                             # in this many different flagged words spells
                             # that way on purpose: those findings get
                             # book_repeat = 2 and rank lower (never removed).
                             # A pair the book's reviewed OCR profile lists
                             # stays an error. 0 (or less): off
    loanword: int = 0        # 1 = on: a long word with a Greek/Latin-like
                             # ending (core.LOANWORD_ENDINGS) whose best
                             # correction
                             # swaps ג/ק, ט/ת, ו/י or ס/צ is likely a
                             # transliteration variant (אונגלוס / אונקלוס):
                             # its score drops by loanword_penalty. Rank only
                             # — the finding and its suggestion stay. Off by
                             # default until measured on a full corpus
    loanword_min_len: int = 6
    loanword_penalty: float = 1.5
    ocr_rare_max: int = 5    # profiled books: scan words up to this frequency
    ocr_ratio: int = 20      # ... requiring correction/word frequency ratio
    real_word: int = 0       # book scan (1 = on): also judge common words by
                             # their neighbours (magiah.realword)
    real_word_odds: float = 5.49  # ... hint when log10 odds that the word
                                  # is wrong reach this (finite; tuned on
                                  # OCR output from outside the corpus)
    real_word_rep: int = 3   # ... a word the book uses this often (>= 1),
                             # and more often than the suggestion, is its
                             # own spelling
    rw_corpus: int = 0       # corpus scan (1 = on): weigh each edit-1 /
                             # spelling-variant correction against the
                             # original between the same neighbours
                             # (realword.posterior; column rw_lr). Rank
                             # only; the context pass counts more n-grams
                             # (roughly 20-40% longer)
    par_corpus: int = 0      # corpus scan (1 = on): parallel passages --
                             # the window of 2 words each side looked up
                             # with the correction / with the original in
                             # other books (columns par_sugg, par_word,
                             # par_vote). Rank only; a longer context pass
    rank_model: int = 0      # calibrate (1 = on): fit a logistic ranking on
                             # the human review labels (rank_model.json,
                             # magiah.rankmodel); rank only. Without
                             # enough labels the hand formula stays
    rank_model_min: int = 200  # ... labels needed to fit it
    workers: int = 3
    n_chunks: int = 24
    whitelist: tuple = ()    # paths of word-list files; listed words are never
                             # flagged (suppression only — never creates flags)
    allow_unread: int = 0    # unreadable input rows a run may skip; the output
                             # is then marked partial (0: none — stop instead)

    # Options that apply only to the run that names them. They are never saved
    # to run_config.json nor restored from it: accepting unreadable rows must
    # be stated again by every run, not inherited silently from an old one.
    PER_RUN = ('allow_unread',)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_run_config(self):
        """The settings run_config.json remembers (no per-run options)."""
        return {k: v for k, v in self.to_dict().items()
                if k not in self.PER_RUN}

    @classmethod
    def from_run_config(cls, d):
        return cls.from_dict({k: v for k, v in (d or {}).items()
                              if k not in cls.PER_RUN})
