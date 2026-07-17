FUNCTION fetchingCustomersAccountsMCC
    LOGIC
        setStore13: UIEngine.SetStore(path = "Page.customerAccounts", value = [])
            output
                getRequest: CoreServices.REST.GetRequest(headers = {
    "content-type": "application/json"
}, url = "customers:listAccessibleCustomers", connectionName = "GOOGLE_API", appCode = "marketingai") AFTER Steps.setStore13.output
                    output
                        setStore: UIEngine.SetStore(path = "Page.googleAccounts", value = Steps.getRequest.output.data.resourceNames)
                            output
                                forEachLoop: System.Loop.ForEachLoop(source = Page.googleAccounts) AFTER Steps.setStore.output
                                    iteration
                                        split: System.String.Split(string = Steps.forEachLoop.iteration.each, searchString = `"/"`)
                                            output
                                                fetchingDetails: Google.FetchingDetails(FetchQuery = `"SELECT customer.id, customer.descriptive_name,customer.manager FROM customer"`, LoginCustomerID = Steps.split.output.result[1], CustomerID = Steps.split.output.result[1])
                                                    output
                                                        if: System.If(condition = Steps.fetchingDetails.output.data)
                                                            true
                                                                if1: System.If(condition = Steps.fetchingDetails.output.data.results[0].customer.manager = true) AFTER Steps.if.true
                                                                    true
                                                                        setStore2: UIEngine.SetStore(path = "Page.customer.id", value = Steps.split.output.result[1]) AFTER Steps.if1.true
                                                                            output
                                                                                setStore1: UIEngine.SetStore(path = "Page.customer.name", value = Steps.fetchingDetails.output.data.results[0].customer.descriptiveName) AFTER Steps.setStore2.output
                                                                                    output
                                                                                        setStore1_Copy_1: UIEngine.SetStore(path = "Page.customer.manager", value = Steps.fetchingDetails.output.data.results[0].customer.manager) AFTER Steps.setStore1.output
                                                                                            output
                                                                                                insertLast: System.Array.InsertLast(source = Page.customerAccounts, element = Page.customer) AFTER Steps.setStore1_Copy_1.output
                                                                                                    output
                                                                                                        setStore3: UIEngine.SetStore(path = "Page.customerAccounts", value = Steps.insertLast.output.result)
                                                                                                            output
                                                                                                                setStore4: UIEngine.SetStore(path = "Page.loginCustomerId", value = Page.customerAccounts[0].id) AFTER Steps.setStore3.output