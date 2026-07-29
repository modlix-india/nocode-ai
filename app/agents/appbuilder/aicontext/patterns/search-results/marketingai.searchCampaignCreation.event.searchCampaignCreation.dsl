FUNCTION searchCampaignCreation
    LOGIC
        jSONParse: System.JSON.JSONParse(source = Page.Campaign)
            output
                setStore3: UIEngine.SetStore(path = "Page.campaignPayload", value = Steps.jSONParse.output.value)
                    output
                        adsOperation: Google.AdsOperation(CustomerID = "<PHONE>", OperationKey = "campaigns", Payload = Page.campaignPayload, LoginCustomerID = "<PHONE>") AFTER Steps.setStore3.output /* creating_search_campaign */
                            error
                                setStore1: UIEngine.SetStore(path = "Page.adBudgetError", value = Steps.adsOperation.error.message)
                            output
                                setStore: UIEngine.SetStore(path = "Page.campaignResponse", value = Steps.adsOperation.output.data)
                                    output
                                        setStore2: UIEngine.SetStore(path = "Page.campaignResourceId", value = Page.campaignResponse.results[0].resourceName) AFTER Steps.setStore.output