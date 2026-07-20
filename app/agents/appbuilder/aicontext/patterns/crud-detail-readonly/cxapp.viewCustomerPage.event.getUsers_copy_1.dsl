FUNCTION getUsers_copy_1
    LOGIC
        setStore2_Copy_1: UIEngine.SetStore(path = "Page.loader", value = true)
            output
                setStore1: UIEngine.SetStore(path = "Page.individualProjectCustomerIds", value = []) AFTER Steps.setStore2_Copy_1.output
                    output
                        readProjectByNameAndClientId: _.readProjectByNameAndClientId() AFTER Steps.setStore1.output
                            output
                                customerByProjectId: cxapp.customerByProjectId(projectId = Page.projectDetails._id) AFTER Steps.readProjectByNameAndClientId.output
                                    error
                                        loaderStopped: UIEngine.SetStore(path = "Page.loader", value = false) AFTER Steps.customerByProjectId.error
                                            output
                                                tableEmptyGrid: UIEngine.SetStore(path = "Page.emptyGrid", value = true) AFTER Steps.loaderStopped.output
                                    output
                                        setStore2: UIEngine.SetStore(path = "Page.customersData", value = Steps.customerByProjectId.output.result)
                                            output
                                                objectKeys: System.Object.ObjectKeys(source = Page.customersData) AFTER Steps.setStore2.output
                                                    output
                                                        forEachLoop: System.Loop.ForEachLoop(source = Steps.objectKeys.output.value)
                                                            iteration
                                                                insert: System.Array.Insert(source = Page.individualProjectCustomerIds, element = {{Steps.forEachLoop.iteration.each}}/1)
                                                                    output
                                                                        setStore8: UIEngine.SetStore(path = "Page.individualProjectCustomerIds", value = Steps.insert.output.result)
                                                                            output
                                                                                setStore3: UIEngine.SetStore(path = "Page.userFilter", value = {
    "condition": {
        "operator": "AND",
        "conditions": [
            {
                "field": "clientId",
                "negate": true
            },
            {
                "field": "id",
                "operator": "IN"
            }
        ]
    }
}) AFTER Steps.setStore8.output
                                                                                    output
                                                                                        setStore4: UIEngine.SetStore(path = "Page.userFilter.condition.conditions[0].value", value = Store.auth.loggedInClientId) AFTER Steps.setStore3.output
                                                                                            output
                                                                                                setStore7: UIEngine.SetStore(path = "Page.userFilter.condition.conditions[1].multiValue", value = Page.individualProjectCustomerIds) AFTER Steps.setStore4.output
                                                                                                    output
                                                                                                        setStore5: UIEngine.SetStore(path = "Page.userFilter.page", value = Page.userData.number ?? 0) AFTER Steps.setStore7.output
                                                                                                            output
                                                                                                                setStore6: UIEngine.SetStore(path = "Page.userFilter.size", value = Page.userData.size ??  5) AFTER Steps.setStore5.output
                                                                                                                    output
                                                                                                                        fetchData: UIEngine.SendData(url = "api/security/users/query", method = "POST", payload = Page.userFilter) AFTER Steps.setStore6.output
                                                                                                                            output
                                                                                                                                setStore: UIEngine.SetStore(path = `'Page.userData'`, value = Steps.fetchData.output.data)
                                                                                                                                    output
                                                                                                                                        setStore2_Copy_2: UIEngine.SetStore(path = "Page.loader", value = false) AFTER Steps.setStore.output