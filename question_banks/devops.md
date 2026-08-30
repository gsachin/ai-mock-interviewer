# DevOps Question Bank

## Blue-green vs canary deployments

Compare blue-green and canary deployment strategies. Expected points: how
traffic is shifted in each, rollback speed versus exposure to bad versions,
infrastructure cost of parallel environments, and where feature flags
complement both.

## Container image layering

Explain how container image layers work and how to keep images small and
builds fast. Expected points: layer caching and ordering, why dependency
install steps must precede code copies, multi-stage builds, and shrinking
the final image.

## Observability with RED and USE

Describe the RED and USE method for monitoring services. Expected points:
rate, errors, duration for request-driven services; utilization,
saturation, errors for resources; SLOs and alerts derived from the four
golden signals.

## CI pipeline caching

How do you keep a CI pipeline fast as a monorepo grows? Expected points:
dependency cache keys that invalidate correctly, caching build artifacts
versus source, parallel job sharding, and treating flaky tests as
incidents.
