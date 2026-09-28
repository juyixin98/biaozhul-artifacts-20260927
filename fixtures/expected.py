"""Hand-computed reference timelines for the synthetic fixtures.

Every number below is derived by hand from the fixture layout (see comments),
NOT by running the parser under test.  Movie-time values are exact rationals
written as (numerator, denominator) pairs in movie-timescale units.

Common layout: ftyp = 24 bytes (8 header + 4 major + 4 minor + 2 brands),
mdat header = 8 bytes, so the first chunk always starts at absolute offset 32.
"""

# ---------------------------------------------------------------------------
# bframes.mp4 — one video track, B-frame reorder via ctts v0 (unsigned)
#   media ts 90000, movie ts 1000, stts (4 x 3000), mdhd duration 21000
#   sizes [100,20,25,90] -> offsets 32,132,152,177
#   DTS [0,3000,6000,9000]; cto [6000,0,3000,9000] -> PTS [6000,3000,9000,18000]
#   implicit edit: seg = 21000 * 1000/90000 = 700/3 (movie units)
#   presentation order by PTS: s1,s0,s2,s3
# ---------------------------------------------------------------------------
BFRAMES = {
    "file": "bframes.mp4",
    "movie_timescale": 1000,
    "tracks": [
        {
            "track_id": 1,
            "handler": "vide",
            "media_timescale": 90000,
            "dts": [0, 3000, 6000, 9000],
            "cto": [6000, 0, 3000, 9000],
            "pts": [6000, 3000, 9000, 18000],
            "byte_offsets": [32, 132, 152, 177],
            "byte_sizes": [100, 20, 25, 90],
            "is_sync": [True, False, False, False],
            "gaps": [],
            "presentations": [  # (sample_index, start, end) in movie units
                (1, (100, 3), (200, 3)),    # pts 3000  -> 3000*1000/90000 = 100/3
                (0, (200, 3), (100, 1)),    # pts 6000  -> 200/3
                (2, (100, 1), (400, 3)),    # pts 9000  -> 100
                (3, (200, 1), (700, 3)),    # pts 18000 -> 200, clipped at 700/3
            ],
            "movie_duration": (700, 3),
        }
    ],
}

# ---------------------------------------------------------------------------
# empty_edit.mp4 — leading empty edit shifts all media by 2000 movie units
#   ts 1000/1000, 3 samples of 1000, sizes [10,11,12] -> offsets 32,42,53
#   elst v0: (seg 2000, media_time -1), (seg 3000, media_time 0)
# ---------------------------------------------------------------------------
EMPTY_EDIT = {
    "file": "empty_edit.mp4",
    "movie_timescale": 1000,
    "tracks": [
        {
            "track_id": 1,
            "handler": "vide",
            "media_timescale": 1000,
            "dts": [0, 1000, 2000],
            "cto": [0, 0, 0],
            "pts": [0, 1000, 2000],
            "byte_offsets": [32, 42, 53],
            "byte_sizes": [10, 11, 12],
            "is_sync": [True, True, True],
            "gaps": [((0, 1), (2000, 1))],
            "presentations": [
                (0, (2000, 1), (3000, 1)),
                (1, (3000, 1), (4000, 1)),
                (2, (4000, 1), (5000, 1)),
            ],
            "movie_duration": (5000, 1),
        }
    ],
}

# ---------------------------------------------------------------------------
# multitrack.mp4 — two tracks with different media timescales
#   movie ts 1000
#   track 1 vide ts 90000: sizes [50,30,60] -> offsets 32,82,112
#     dts [0,3000,6000], ctts v1 [3000,-3000,3000] -> pts [3000,0,9000]
#     mdhd dur 12000 -> implicit seg 400/3
#   track 2 soun ts 48000: 6 samples x 8 bytes (constant stsz) -> chunk at
#     32+140=172, offsets 172,180,...,212; dts = i*1024; mdhd dur 6144
#     sample duration in movie units: 1024*1000/48000 = 64/3
# ---------------------------------------------------------------------------
MULTITRACK = {
    "file": "multitrack.mp4",
    "movie_timescale": 1000,
    "tracks": [
        {
            "track_id": 1,
            "handler": "vide",
            "media_timescale": 90000,
            "dts": [0, 3000, 6000],
            "cto": [3000, -3000, 3000],
            "pts": [3000, 0, 9000],
            "byte_offsets": [32, 82, 112],
            "byte_sizes": [50, 30, 60],
            "is_sync": [True, True, True],
            "gaps": [],
            "presentations": [
                (1, (0, 1), (100, 3)),
                (0, (100, 3), (200, 3)),
                (2, (100, 1), (400, 3)),
            ],
            "movie_duration": (400, 3),
        },
        {
            "track_id": 2,
            "handler": "soun",
            "media_timescale": 48000,
            "dts": [0, 1024, 2048, 3072, 4096, 5120],
            "cto": [0, 0, 0, 0, 0, 0],
            "pts": [0, 1024, 2048, 3072, 4096, 5120],
            "byte_offsets": [172, 180, 188, 196, 204, 212],
            "byte_sizes": [8, 8, 8, 8, 8, 8],
            "is_sync": [True, True, True, True, True, True],
            "gaps": [],
            "presentations": [
                (0, (0, 1), (64, 3)),
                (1, (64, 3), (128, 3)),
                (2, (128, 3), (64, 1)),
                (3, (64, 1), (256, 3)),
                (4, (256, 3), (320, 3)),
                (5, (320, 3), (128, 1)),
            ],
            "movie_duration": (128, 1),
        },
    ],
}

# ---------------------------------------------------------------------------
# ctts_signed.mp4 — ctts v1 negative offsets + elst v1 + co64 chunk offsets
#   ts 1000/1000, sizes [40,41,42] -> offsets 32,72,113
#   dts [0,1000,2000], ctts v1 [2000,-2000,4000] -> pts [2000,-1000,6000]
#   elst v1: (seg 8000, media_time -1000) -> media window [-1000, 7000)
# ---------------------------------------------------------------------------
CTTS_SIGNED = {
    "file": "ctts_signed.mp4",
    "movie_timescale": 1000,
    "tracks": [
        {
            "track_id": 1,
            "handler": "vide",
            "media_timescale": 1000,
            "dts": [0, 1000, 2000],
            "cto": [2000, -2000, 4000],
            "pts": [2000, -1000, 6000],
            "byte_offsets": [32, 72, 113],
            "byte_sizes": [40, 41, 42],
            "is_sync": [True, True, True],
            "gaps": [],
            "presentations": [
                (1, (0, 1), (1000, 1)),      # pts -1000 -> movie 0
                (0, (3000, 1), (4000, 1)),   # pts 2000  -> movie 3000
                (2, (7000, 1), (8000, 1)),   # pts 6000  -> movie 7000
            ],
            "movie_duration": (8000, 1),
        }
    ],
}

# ---------------------------------------------------------------------------
# trim.mp4 — edit window cuts into the stream; two chunks via stsc
#   ts 1000/1000, sizes [10,20,30,40], stsc [(1,3,1),(2,1,1)]
#   chunk offsets [32, 92] -> sample offsets 32,42,62,92
#   elst v0: (seg 1500, media_time 500) -> media window [500, 2000)
#   only sample 1 (pts 1000) is presented; movie [0,500) is uncovered
# ---------------------------------------------------------------------------
TRIM = {
    "file": "trim.mp4",
    "movie_timescale": 1000,
    "tracks": [
        {
            "track_id": 1,
            "handler": "vide",
            "media_timescale": 1000,
            "dts": [0, 1000, 2000, 3000],
            "cto": [0, 0, 0, 0],
            "pts": [0, 1000, 2000, 3000],
            "byte_offsets": [32, 42, 62, 92],
            "byte_sizes": [10, 20, 30, 40],
            "is_sync": [True, True, True, True],
            "gaps": [],
            "presentations": [
                (1, (500, 1), (1500, 1)),
            ],
            "movie_duration": (1500, 1),
        }
    ],
}

GOOD_FIXTURES = [BFRAMES, EMPTY_EDIT, MULTITRACK, CTTS_SIGNED, TRIM]

# Failure fixtures and the error category each must produce.
BAD_FIXTURES = {
    "bad_boxlen.mp4": "box_out_of_bounds",
    "fragmented.mp4": "unsupported_fragmented_layout",
    "encrypted.mp4": "unsupported_encryption",
    "bad_stts.mp4": "malformed_box",
    "bad_rate.mp4": "unsupported_feature",
}
