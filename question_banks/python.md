# Python Question Bank

## Python data types

Explain Python's built-in data types and when to use them. Expected points: integers, floats, booleans, strings, lists, tuples, sets, dictionaries, `None`, mutability, type conversion, and choosing data structures based on the required operations.

## List vs tuple

Compare lists and tuples in Python. Expected points: mutability, syntax, performance characteristics, memory considerations, hashability, use as dictionary keys, API design, and when immutable data is preferable.

## Dictionary and set

Explain how dictionaries and sets work in Python. Expected points: hashing, key uniqueness, average lookup complexity, insertion and deletion, hashable keys, set operations, ordering guarantees in modern Python, and appropriate use cases.

## Mutable vs immutable objects

Explain mutable and immutable objects in Python. Expected points: examples such as lists, dictionaries, sets, integers, strings, and tuples, object identity, reassignment versus mutation, side effects, function arguments, and implications for application design.

## Python scope

Explain how variable scope works in Python. Expected points: LEGB rule, local, enclosing, global, and built-in scopes, `global`, `nonlocal`, nested functions, variable shadowing, and avoiding unnecessary global state.

## List comprehensions

Explain list comprehensions and when they should or should not be used. Expected points: concise transformation and filtering, readability, nested comprehensions, conditional expressions, generator expressions, performance considerations, and avoiding overly complex comprehensions.

## Iterators and generators

Compare iterators and generators in Python. Expected points: iterable versus iterator, `iter()`, `next()`, `yield`, lazy evaluation, memory efficiency, generator expressions, streaming large datasets, and situations where eager collections are preferable.

## Decorators

Explain Python decorators and how they work. Expected points: higher-order functions, functions accepting functions, wrapper functions, `@decorator` syntax, `functools.wraps`, preserving metadata, practical use cases such as logging and authorization, and avoiding hidden side effects.

## Exception handling

How should exceptions be handled in production Python applications? Expected points: `try`, `except`, `else`, `finally`, specific exception types, custom exceptions, exception propagation, logging, cleanup, avoiding bare `except`, and not using exceptions to control normal program flow unnecessarily.

## Context managers

Explain Python context managers and the `with` statement. Expected points: resource acquisition and cleanup, `__enter__`, `__exit__`, file handling, database connections, locks, `contextlib`, exception handling during cleanup, and why context managers improve reliability.

## Python memory management

Explain how Python manages memory. Expected points: object allocation, reference counting in CPython, cyclic garbage collection, object lifetime, references, memory leaks caused by application-level references, generators, caches, and tools for investigating memory usage.

## Multithreading vs multiprocessing

Compare threading and multiprocessing in Python. Expected points: concurrency versus parallelism, CPython GIL, CPU-bound versus I/O-bound workloads, process isolation, communication overhead, thread safety, synchronization, and appropriate use cases for each model.

## Asyncio

Explain asynchronous programming with `asyncio`. Expected points: event loop, coroutines, `async`, `await`, tasks, non-blocking I/O, concurrency, cancellation, timeouts, CPU-bound limitations, and when asynchronous programming is preferable to threads or processes.

## Python packaging and dependencies

How would you manage dependencies for a production Python application? Expected points: virtual environments, `pyproject.toml`, dependency pinning, lock files, package versions, reproducible builds, dependency security, development versus production dependencies, and avoiding dependency conflicts.

## Python testing

How do you build a reliable testing strategy for a Python application? Expected points: unit tests, integration tests, end-to-end tests, pytest, fixtures, mocking, test isolation, parametrization, code coverage, deterministic tests, CI execution, and treating flaky tests as engineering issues.