# System Design Question Bank

## Design a rate limiter

Design a rate limiter for a public API that must serve millions of users.
Expected points: token bucket versus sliding window algorithms, per-user
keys, Redis counters with atomic increments, backpressure behavior, and
returning 429 responses with retry-after headers.

## Consistent hashing

Explain consistent hashing and why it minimizes reshuffling when nodes join
or leave a ring. Expected points: the hash ring, virtual nodes for even
distribution, the fraction of keys that move on node change, and how this
applies to distributed caches and sharded databases.

## Design a URL shortener

Design a URL shortener like bit.ly. Expected points: id generation and
base62 encoding, collision handling, database partitioning, caching hot
short codes, and analytics without blocking the redirect path.

## Idempotency keys

When and why do distributed systems need idempotency keys? Expected points:
retry storms from at-least-once delivery, storing keys with request
fingerprints, TTL policies, and how payment APIs enforce exactly-once
semantics.
