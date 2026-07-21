FUNCTION createBudget
    LOGIC
        jSONParse: System.JSON.JSONParse(source = Page.Budget)
            output
                setStore3: UIEngine.SetStore(path = "Page.budgetPayload", value = Steps.jSONParse.output.value)
                    output
                        adsOperation: Google.AdsOperation(CustomerID = "<PHONE>", OperationKey = "campaignBudgets", Payload = Page.budgetPayload, LoginCustomerID = "<PHONE>") AFTER Steps.setStore3.output /* creating_no_shared_budget */
                            error
                                setStore1: UIEngine.SetStore(path = "Page.adBudgetError", value = Steps.adsOperation.error.message)
                            output
                                setStore: UIEngine.SetStore(path = "Page.adBudgetResponse", value = Steps.adsOperation.output.data)
                                    output
                                        setStore2: UIEngine.SetStore(path = "Page.budgetResourceId", value = Page.adBudgetResponse.results[0].resourceName) AFTER Steps.setStore.output /* budget_resource_id */