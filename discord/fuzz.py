from rapidfuzz.fuzz import ratio
from rapidfuzz.process import extractOne
from rapidfuzz.utils import default_process
from runtimeopt import offloaded
from typing import TYPE_CHECKING, Iterable, Sequence, Hashable
from melanie.redis import get_redis
import msgpack
import asyncio
from xxhash import xxh3_64_hexdigest

if TYPE_CHECKING:
    from rapidfuzz.process_py import extractOne
else:
    from rapidfuzz.process_cpp import extractOne


@offloaded
def do_extract(query, choices):
    return extractOne(query, choices, scorer=ratio, processor=default_process, score_cutoff=80)


@offloaded
async def extract(query: str, choices: Iterable) -> tuple[Sequence[Hashable], int | float, int] | None:
    redis = get_redis()
    async with asyncio.timeout(30):
        key = f"search_extract:{xxh3_64_hexdigest(f'{str(query)}:{[str().join(str(x) for x in choices)]}')}"
        async with redis.get_lock(key, timeout=25):
            cached = await redis.get(key)
            if not cached:
                cached = await do_extract(query, choices)
                await redis.set(key, msgpack.packb(cached), ex=3600)
            else:
                cached = msgpack.unpackb(cached)
            return cached
