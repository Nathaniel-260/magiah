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
    foreign_ratio: float = 0.35  # skip lines with this share of uncommon words
                                 # (Judeo-Arabic / badly garbled passages)
    workers: int = 3
    n_chunks: int = 24
    whitelist: tuple = ()    # paths of word-list files; listed words are never
                             # flagged (suppression only — never creates flags)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
