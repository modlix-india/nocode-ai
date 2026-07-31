FUNCTION dropdownSelectEvent
    LOGIC
        deletingTheCampaignsListkey: UIEngine.SetStore(path = "Page.listCampaigns", value = null, deleteKey = true)
            output
                initialisingCampaignsToemptyArray: UIEngine.SetStore(path = "Page.campaigns", value = []) AFTER Steps.deletingTheCampaignsListkey.output
                    output
                        initialisingLoader: UIEngine.SetStore(path = "Page.showLoader", value = "showLoader") AFTER Steps.initialisingCampaignsToemptyArray.output
                            output
                                initialisingThePageCursor: UIEngine.SetStore(path = "Page.pageCursor", value = "") AFTER Steps.initialisingLoader.output
                                    output
                                        settingSearchParameter: UIEngine.SetStore(path = "Page.searchParameter", value = "") AFTER Steps.initialisingThePageCursor.output
                                            output
                                                fetch_functionality: _.fetch_functionality() AFTER Steps.settingSearchParameter.output
                                                    output
                                                        if: System.If(condition = Page.campaigns.length) AFTER Steps.fetch_functionality.output
                                                            true
                                                                settingThePagenumberr: UIEngine.SetStore(path = `'Page.currentPageNumber'`, value = 1) AFTER Steps.if.true
                                                                    output
                                                                        settingConditionForemptyCampaigns: UIEngine.SetStore(path = `'Page.showCampaigns'`, value = `'showCampaigns'`) AFTER Steps.settingThePagenumberr.output
                                                                            output
                                                                                forShowingTheCampaignsList: UIEngine.SetStore(path = "Page.listCampaigns", value = `'listing'`) AFTER Steps.settingConditionForemptyCampaigns.output
                                                            false
                                                                deletingShowCampaignsKey: UIEngine.SetStore(path = `'Page.showCampaigns'`, value = ``, deleteKey = true) AFTER Steps.if.false
                                                            output
                                                                disableLoader: UIEngine.SetStore(path = "Page.showLoader", value = null, deleteKey = true) AFTER Steps.if.output