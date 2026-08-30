# iOS Engineering Question Bank

## Swift concurrency and actors

Explain how Swift actors prevent data races and where actor isolation does
not apply. Expected points: serialized access to actor state, await points
suspending rather than blocking, Sendable checking, and moving UI work to
the MainActor.

## Retain cycles and closures

Describe a retain cycle caused by a closure capturing self and how to break
it. Expected points: strong capture defaults, weak versus unowned with
lifetime reasoning, capture lists, and why escaping closures are the usual
culprit.

## Auto Layout vs SwiftUI

Compare Auto Layout constraint solving with SwiftUI's declarative layout.
Expected points: constraint equations and ambiguity, layout passes versus
re-rendering, performance on complex hierarchies, and when each is the
right choice for a production app.

## iOS app lifecycle

Walk through the modern iOS app lifecycle and where background work fits.
Expected points: scene phases, application state transitions, background
tasks and their time budgets, and restoring UI state after termination.
