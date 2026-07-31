FUNCTION onclickFilter
    LOGIC
        filter: UIEngine.SetStore(path = "Page.filter", value = not Page.filter)
            output
                collapse: UIEngine.SetStore(path = "Page.collapse", value = true) AFTER Steps.filter.output
                    output
                        columns: UIEngine.SetStore(path = "Page.columns", value = false) AFTER Steps.collapse.output
        if1: System.If(condition = Page.usersQuery)
            false
                make: System.Make(resultShape = {
    "condition": {
        "operator": "AND",
        "conditions": [
            {
                "field": "appCode",
                "value": "leadzump",
                "operator": "EQUALS"
            },
            {
                "field": "clientId",
                "operator": "EQUALS",
                "value": "{{Store.auth.client.id}}"
            }
        ]
    },
    "page": 0,
    "size": 25,
    "sort": {
        "property": "firstName",
        "direction": "ASC"
    }
}) AFTER Steps.if1.false
                    output
                        setStore: UIEngine.SetStore(path = "Page.usersQuery", value = Steps.make.output.value)
        if: System.If(condition = Page.filterContent.taskPriority.length = 0)
            true
                filteDataNew: UIEngine.SetStore(path = "Page.filterContent.taskPriority", value = [{
    "name": "HIGH",
    "field": "priority",
    "id": "HIGH"
}, {
    "name": "MEDIUM",
    "field": "priority",
    "id": "MEDIUM"
}, {
    "name": "LOW",
    "field": "priority",
    "id": "LOW"
}]) AFTER Steps.if.true