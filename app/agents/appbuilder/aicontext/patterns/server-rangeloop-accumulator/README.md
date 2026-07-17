# server-rangeloop-accumulator

RangeLoop accumulating a result (like the fibonacci sample).

**Notes:**

Look at: `System.Context.Create` + `System.Context.Set` to initialize an empty array accumulator before the loop; `System.Loop.RangeLoop` with `to = Arguments.n`; `System.Array.InsertLast` inside the iteration writing back to `Context.a` via `System.Context.Set`; final `System.GenerateEvent` after the loop's `output` branch emitting `Context.a` as an expression.

**Entity type:** `server_function`

## Samples

- **appbuilder** / `Test.fibonacci` (v14, clientCode=SYSTEM)
  - [appbuilder.Test.fibonacci.json](appbuilder.Test.fibonacci.json)
  - [appbuilder.Test.fibonacci.dsl](appbuilder.Test.fibonacci.dsl)
