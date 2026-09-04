# JavaScript Question Bank

## JavaScript execution model

Explain how JavaScript executes code in the browser. Expected points: call stack, execution contexts, synchronous execution, Web APIs, task queues, microtasks, event loop, and how asynchronous operations return control to the runtime.

## var, let, and const

Compare `var`, `let`, and `const`. Expected points: function versus block scope, hoisting, temporal dead zone, redeclaration, reassignment, global behavior, and why `let` and `const` are generally preferred in modern JavaScript.

## Hoisting

Explain hoisting in JavaScript. Expected points: how declarations are processed before execution, differences between `var`, `let`, `const`, and function declarations, temporal dead zone, and why hoisting should not be confused with physically moving code.

## Closures

What is a closure and where is it useful? Expected points: lexical scope, function retaining access to outer variables, private state, callbacks, factory functions, event handlers, common memory considerations, and practical examples.

## Scope and lexical environment

Explain JavaScript scope and lexical environments. Expected points: global scope, function scope, block scope, lexical lookup, nested scopes, shadowing, closures, and differences between lexical scope and dynamic scope.

## this keyword

Explain how `this` is determined in JavaScript. Expected points: method calls, standalone function calls, constructor calls, `call`, `apply`, `bind`, arrow functions, strict mode, and why `this` depends on invocation context rather than where a normal function is defined.

## Arrow functions

Compare arrow functions with regular functions. Expected points: lexical `this`, lack of their own `arguments`, constructor limitations, concise syntax, callback use cases, and situations where regular functions are more appropriate.

## Prototypes and inheritance

Explain JavaScript's prototype-based inheritance. Expected points: prototype chain, `Object.create`, constructor functions, `prototype`, property lookup, classes as syntax over prototype mechanisms, and differences from classical inheritance models.

## Classes

Explain how JavaScript classes work. Expected points: constructors, instance methods, static methods, inheritance, `extends`, `super`, private fields, prototype behavior, and the relationship between classes and JavaScript's prototype system.

## Promises

Explain JavaScript Promises and their lifecycle. Expected points: pending, fulfilled, rejected states, `then`, `catch`, `finally`, chaining, error propagation, `Promise.all`, `Promise.allSettled`, `Promise.race`, and `Promise.any`.

## Async and await

Explain how `async` and `await` simplify asynchronous JavaScript. Expected points: async functions returning Promises, awaiting Promise settlement, sequential versus parallel execution, error handling with `try/catch`, and avoiding unnecessary sequential awaits.

## Event loop

Explain the JavaScript event loop in detail. Expected points: call stack, macrotasks, microtasks, timers, Promise callbacks, rendering opportunities, execution order, and why microtasks can delay other work when continuously scheduled.

## Event delegation

Explain event delegation and why it is useful. Expected points: event bubbling, handling events from a common ancestor, dynamic elements, reducing event listeners, `target` versus `currentTarget`, and cases where delegation may not be appropriate.

## Deep copy vs shallow copy

Compare shallow and deep copying of JavaScript objects. Expected points: primitive versus reference values, object and array references, spread syntax, `Object.assign`, structured cloning, limitations of JSON serialization, circular references, and selecting an appropriate cloning strategy.

## JavaScript memory management

Explain how JavaScript manages memory. Expected points: allocation, references, garbage collection, reachability, common causes of memory leaks, event listeners, timers, closures, detached DOM nodes, and techniques for diagnosing memory problems in production applications.