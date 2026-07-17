FUNCTION campaignDetails
    LOGIC
        if2: System.If(condition = Page.campaignId)
            true
                concatenate: System.String.Concatenate(value = "SELECT campaign.id, campaign.name,campaign_budget.amount_micros, campaign.status, campaign.advertising_channel_type, campaign.start_date, campaign.end_date FROM campaign ", value = "WHERE campaign.status != 'REMOVED' AND campaign.id = ", value = `"{{Page.campaignId}}"`) AFTER Steps.if2.true
                    output
                        setStore2: UIEngine.SetStore(path = "Page.fetchingCamAndBudDetails", value = Steps.concatenate.output.value)
                            output
                                fetchingDetails: Google.FetchingDetails(FetchQuery = Page.fetchingCamAndBudDetails, CustomerID = Store.urlDetails.queryParameters.customerId, LoginCustomerID = Store.urlDetails.queryParameters.loginCustomerId) AFTER Steps.setStore2.output
                                    output
                                        setStore_Copy_1: UIEngine.SetStore(path = `'Page.campaignDetails'`, value = Steps.fetchingDetails.output.data)
                                            output
                                                setStore1: UIEngine.SetStore(path = "Page.campaignType", value = `Page.campaignDetails.results[0].campaign.advertisingChannelType + ' CAMPAIGN'`) AFTER Steps.setStore_Copy_1.output
                                                    output
                                                        setStore: UIEngine.SetStore(path = "Page.campaignName", value = Page.campaignDetails.results[0].campaign.name) AFTER Steps.setStore1.output
                                                            output
                                                                if1: System.If(condition = Page.campaignDetails.results[0].campaignBudget.amountMicros) AFTER Steps.setStore.output
                                                                    true
                                                                        setStore3: UIEngine.SetStore(path = "Page.dailyBudget", value = {{Page.campaignDetails.results[0].campaignBudget.amountMicros}} / 1000000) AFTER Steps.if1.true
                                                                            output
                                                                                concatenate1: System.String.Concatenate(value = "SELECT campaign_criterion.campaign, campaign_criterion.criterion_id, campaign_criterion.type,campaign.id, campaign_criterion.status,campaign_criterion.location_group ,campaign_criterion.location.geo_target_constant FROM campaign_criterion ", value = "WHERE campaign_criterion.type = 'LOCATION' AND campaign.id =  ", value = `"{{Page.campaignId}}"`) AFTER Steps.setStore3.output
                                                                                    output
                                                                                        setStore2_Copy_1: UIEngine.SetStore(path = "Page.campaignCriterionDetails", value = Steps.concatenate1.output.value)
                                                                                            output
                                                                                                fetchingDetails1: Google.FetchingDetails(FetchQuery = Page.campaignCriterionDetails, CustomerID = Store.urlDetails.queryParameters.customerId, LoginCustomerID = Store.urlDetails.queryParameters.loginCustomerId) AFTER Steps.setStore2_Copy_1.output
                                                                                                    output
                                                                                                        setStore_Copy_1_Copy_1: UIEngine.SetStore(path = `'Page.campaignCriterionDetails'`, value = Steps.fetchingDetails1.output.data)
                                                                                                            output
                                                                                                                if: System.If(condition = Page.campaignCriterionDetails.results) AFTER Steps.setStore_Copy_1_Copy_1.output
                                                                                                                    true
                                                                                                                        fetchingGeoLocationConstant: _.fetchingGeoLocationConstant() AFTER Steps.if.true
            false
                message: UIEngine.Message(msg = "Please select the specific campaign in campaign table") AFTER Steps.if2.false