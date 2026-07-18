# Cache Clearing via `/reload` — What It Does

## Summary

The `/reload` command (admin-only) clears an **in-memory TTL cache of search results** (`SimpleTTLCache`). It **does not** reload the database, re-import products, or restart the bot.

## Where It Lives

**File**: `app/handlers.py`  
**Definition**: `_reload` handler at line ~269 (inside `register_handlers()`)

```python
@router.message(Command("reload"))
async def _reload(message: Message) -> None:
    if not await _require_admin(message):
        return
    cache.clear()
    await _answer(message, "Ок. Кеш очищено.")
```

## What Cache Exactly?

The `cache` is a `SimpleTTLCache` instance created at the top of `register_handlers()`:

```python
cache = SimpleTTLCache(ttl_seconds=30, max_items=300)
```

The class is defined in the same file (`app/handlers.py`; see `class SimpleTTLCache`):

- **Storage**: in-memory dict (`self._items: dict[str, CacheEntry]`)
- **Keys**: formatted as `"admin:<query>"` or `"user:<query>"` (see `_cache_key()`)
- **Values**: pre-formatted HTML strings of search results (product lines)
- **TTL**: 30 seconds per entry (expired entries are skipped and lazily removed)
- **Max items**: 300 (LRU-like eviction when full — removes expired first, else removes one oldest)

## What Clearing Does

`cache.clear()` calls `self._items.clear()` — **removes all cached search responses unconditionally**.

After `/reload`:

- The next search query from any user (admin or allowed) will **hit the SQLite database fresh** instead of returning a cached string.
- There is no impact on the database, user sessions, or bot state.

## When Should an Admin Use `/reload`?

- After **manually updating the product database** outside the bot (e.g., direct SQLite edits, or running `scripts/import_html.py` separately).
- If old search results are suspect (stale) and the admin doesn't want to wait 30 seconds for TTL expiry.
- **Note**: When products are imported via the **Upload HTML document** handler (`F.document`), the cache is **automatically cleared** after the import completes (both on success and on error). So `/reload` is only needed for out-of-band database changes.

## Visual Flow

```
User sends /reload → _require_admin checks role → cache.clear() → "Ок. Кеш очищено."
```

No database writes, no re-imports, no restarts.

## Internal Class: `SimpleTTLCache`

| Method            | Behavior                                                                                                                     |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `get(key)`        | Returns cached text if not expired; else removes entry and returns `None`                                                    |
| `set(key, value)` | Stores with `expires_at = now + ttl_seconds`. Evicts if over `max_items` (removes expired first, else removes any one entry) |
| `clear()`         | Removes ALL entries unconditionally                                                                                          |

## Error Recovery

If `/reload` raises an exception (very unlikely), the admin still gets "Доступ заборонено" for non-admin, or the exception propagates up to the aiogram dispatcher's error handlers. No special recovery is needed because cache is in-memory only.
