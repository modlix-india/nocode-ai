FUNCTION fibonacci
    NAMESPACE Test
    PARAMETERS
        n AS {"type": "INTEGER", "version": 1}
    EVENTS
        output
            result AS ARRAY OF INTEGER
    LOGIC
        create: System.Context.Create(name = "a", schema = {
    "type": "ARRAY",
    "items": {
        "type": "INTEGER"
    }
})
            output
                set2: System.Context.Set(name = "Context.a", value = []) AFTER Steps.create.output
                    output
                        rangeLoop: System.Loop.RangeLoop(to = Arguments.n) AFTER Steps.set2.output
                            iteration
                                if: System.If(condition = Steps.rangeLoop.iteration.index < 2)
                                    true
                                        trueInsert: System.Array.InsertLast(source = Context.a, element = Steps.rangeLoop.iteration.index) AFTER Steps.if.true
                                            output
                                                set: System.Context.Set(name = "Context.a", value = Steps.trueInsert.output.result)
                                    false
                                        falseInsert: System.Array.InsertLast(source = Context.a, element = Context.a[Steps.rangeLoop.iteration.index - 1] + Context.a[Steps.rangeLoop.iteration.index - 2]) AFTER Steps.if.false
                                            output
                                                set1: System.Context.Set(name = "Context.a", value = Steps.falseInsert.output.result)
                            output
                                generateEvent: System.GenerateEvent(results = {
    "name": "result",
    "value": {
        "isExpression": true,
        "value": "Context.a"
    }
}) AFTER Steps.rangeLoop.output