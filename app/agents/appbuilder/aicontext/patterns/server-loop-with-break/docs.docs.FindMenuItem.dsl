FUNCTION FindMenuItem
    NAMESPACE docs
    PARAMETERS
        menuItems AS ARRAY OF OBJECT
        findKey AS {"type": "STRING", "version": 1}
    EVENTS
        output
            menuItemHirearchy AS ARRAY OF {"ref": "docs.MenuItem"}
    LOGIC
        getAuthentication: CoreServices.SecurityContext.GetAuthentication()
        create: System.Context.Create(name = "return", schema = {
    "type": "ARRAY",
    "version": 1,
    "items": {
        "ref": "docs.MenuItem"
    }
})
            output
                set: System.Context.Set(value = [], name = "Context.return") AFTER Steps.create.output
                    output
                        forEachLoop: System.Loop.ForEachLoop(source = Arguments.menuItems) AFTER Steps.set.output
                            iteration
                                if: System.If(condition = (Steps.forEachLoop.iteration.each.uniqueKey = Arguments.findKey) and (not Steps.forEachLoop.iteration.each.isPrivate or Steps.getAuthentication.output.auth.isAuthenticated))
                                    true
                                        addFirst: System.Array.AddFirst(source = Context.return, element = Steps.forEachLoop.iteration.each) AFTER Steps.if.true
                                            output
                                                set1: System.Context.Set(value = Steps.addFirst.output.result, name = "Context.return")
                                                    output
                                                        break: System.Loop.Break(stepName = "forEachLoop") AFTER Steps.set1.output
                                    false
                                        if1: System.If(condition = ((Steps.forEachLoop.iteration.each.subMenu.length ?? -1) + 1) > 0) AFTER Steps.if.false
                                            true
                                                findMenuItem: docs.FindMenuItem(menuItems = Steps.forEachLoop.iteration.each.subMenu, findKey = Arguments.findKey) AFTER Steps.if1.true
                                                    output
                                                        if2: System.If(condition = Steps.findMenuItem.output.menuItemHirearchy.length) AFTER Steps.findMenuItem.output
                                                            true
                                                                addFirst1: System.Array.AddFirst(source = Steps.findMenuItem.output.menuItemHirearchy, element = Steps.forEachLoop.iteration.each) AFTER Steps.if2.true
                                                                    output
                                                                        set2: System.Context.Set(value = Steps.addFirst1.output.result, name = "Context.return")
                                                                            output
                                                                                break1: System.Loop.Break(stepName = "forEachLoop") AFTER Steps.set2.output
                            output
                                generateEvent: System.GenerateEvent(results = {
    "name": "menuItemHirearchy",
    "value": {
        "isExpression": true,
        "value": "Context.return"
    }
}) AFTER Steps.forEachLoop.output