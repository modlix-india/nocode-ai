FUNCTION searchCampaignCriterion
    LOGIC
        jSONParse: System.JSON.JSONParse(source = Page.campaignCriterion)
            output
                setStore2: UIEngine.SetStore(path = "Page.campaignCriterionPayload", value = Steps.jSONParse.output.value)
                    output
                        adsOperation: Google.AdsOperation(CustomerID = "<PHONE>", OperationKey = "campaignCriteria", Payload = Page.campaignCriterionPayload, LoginCustomerID = "<PHONE>") AFTER Steps.setStore2.output /* creating_search_campaign */
                            error
                                setStore1: UIEngine.SetStore(path = "Page.campaignCriterionError", value = Steps.adsOperation.error.message)
                            output
                                setStore: UIEngine.SetStore(path = "Page.campaignCriterionPayload", value = Steps.adsOperation.output.data)