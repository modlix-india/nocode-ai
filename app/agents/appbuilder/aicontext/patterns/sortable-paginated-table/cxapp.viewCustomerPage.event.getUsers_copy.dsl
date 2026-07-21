FUNCTION getUsers_copy
    LOGIC
        setStore3: UIEngine.SetStore(path = "Page.userFilter", value = {
    "condition": {
        "field": "clientId",
        "negate": true
    }
})
            output
                setStore4: UIEngine.SetStore(path = "Page.userFilter.condition.value", value = Store.auth.loggedInClientId) AFTER Steps.setStore3.output
                    output
                        setStore5: UIEngine.SetStore(path = "Page.userFilter.page", value = Page.userData.number ?? 0) AFTER Steps.setStore4.output
                            output
                                setStore6: UIEngine.SetStore(path = "Page.userFilter.size", value = Page.userData.size ??  5) AFTER Steps.setStore5.output
                                    output
                                        fetchData: UIEngine.SendData(url = "api/security/users/query", method = "POST", payload = Page.userFilter, headers = {
    "Authorization": {
        "location": {
            "expression": "LocalStore.AuthToken",
            "type": "EXPRESSION"
        }
    },
    "clientCode": {
        "location": {
            "expression": "Store.auth.loggedInClientCode",
            "type": "EXPRESSION"
        }
    }
}, queryParams = {
    "appCode": {
        "type": "VALUE",
        "value": "cxapp"
    }
}) AFTER Steps.setStore6.output
                                            output
                                                setStore11: UIEngine.SetStore(path = "Page.custIds", value = []) AFTER Steps.fetchData.output
                                                setStore: UIEngine.SetStore(path = `'Page.userData'`, value = Steps.fetchData.output.data)
                                                    output
                                                        if1: System.If(condition = Page.userData.content != undefined) AFTER Steps.setStore.output
                                                            true
                                                                forEachLoop: System.Loop.ForEachLoop(source = Page.userData.content) AFTER Steps.if1.true
                                                                    iteration
                                                                        setStore10: UIEngine.SetStore(path = `'Page.custIds[{{Steps.forEachLoop.iteration.index}}]'`, value = Steps.forEachLoop.iteration.each.id)
                                                                    output
                                                                        getAllBookings: rim.getAllBookings(userIds = Page.custIds) AFTER Steps.forEachLoop.output
                                                                            output
                                                                                forEachLoop1: System.Loop.ForEachLoop(source = Steps.getAllBookings.output.allBookings)
                                                                                    iteration
                                                                                        if: System.If(condition = Page.customersData.{{Steps.forEachLoop1.iteration.each.userId}}.sqftAllocated != 0)
                                                                                            false
                                                                                                setStore1: UIEngine.SetStore(path = `'Page.customersData.{{Steps.forEachLoop1.iteration.each.userId}}.sqftAllocated'`, value = Steps.forEachLoop1.iteration.each.sqftAllocated) AFTER Steps.if.false
                                                                                        if_Copy_1: System.If(condition = Page.customersData.{{Steps.forEachLoop1.iteration.each.userId}}.amountInvested != 0)
                                                                                            false
                                                                                                setStore1_Copy_1: UIEngine.SetStore(path = `'Page.customersData.{{Steps.forEachLoop1.iteration.each.userId}}.amountInvested'`, value = Steps.forEachLoop1.iteration.each.amountInvested) AFTER Steps.if_Copy_1.false
                activatingNotification: _.activatingNotification() AFTER Steps.setStore3.output