FUNCTION pageOnLoadEvent
    LOGIC
        setStore_Copy_1: UIEngine.SetStore(path = "Page.filterItemEnter", value = -1)
            output
                initialisingindextonegative: UIEngine.SetStore(path = "Page.idIconEnter", value = -1) AFTER Steps.setStore_Copy_1.output
        setStore4: UIEngine.SetStore(path = "Page.campSteps", value = 0)
            output
                addbasicDetailsColor_Copy_1: UIEngine.SetStore(path = "Page.addBasicDetailsBackgroundColor", value = "#4335A712") AFTER Steps.setStore4.output
                    output
                        selectiveObjectiveColor_Copy_1: UIEngine.SetStore(path = "Page.selectObjectiveBackgroundColor", value = "#fff") AFTER Steps.addbasicDetailsColor_Copy_1.output
                            output
                                addBudgetColor_Copy_1: UIEngine.SetStore(path = "Page.addBudgetBackgroundColor", value = "#fff") AFTER Steps.selectiveObjectiveColor_Copy_1.output
                                    output
                                        setStore6: UIEngine.SetStore(path = "Page.completedArray", value = []) AFTER Steps.addBudgetColor_Copy_1.output
        initialisingThePageCursor: UIEngine.SetStore(path = "Page.pageCursor", value = "")
        initialisingItemsPerPage: UIEngine.SetStore(path = "Page.itemsPerPage", value = "10")
        settingSearchParameter: UIEngine.SetStore(path = "Page.searchParameter", value = "")
        fetchingPersonalisationData: _.fetchingPersonalisationData()
            output
                settingfilterdata: UIEngine.SetStore(path = "Page.timingsData", value = [{
    "key": "today",
    "value": "Today"
}, {
    "key": "yesterday",
    "value": "Yesterday"
}, {
    "key": "last_7d",
    "value": "Last 7 Days"
}, {
    "key": "last_30d",
    "value": "Last 30 Days"
}, {
    "key": "this_month",
    "value": "This Month"
}, {
    "key": "last_month",
    "value": "Last Month"
}, {
    "key": "maximum",
    "value": "Maximum"
}]) AFTER Steps.fetchingPersonalisationData.output
                    output
                        setdatePresetvalue: UIEngine.SetStore(path = "Page.datePresetValue", value = "This Month") AFTER Steps.settingfilterdata.output
                            output
                                settingDatePreset: UIEngine.SetStore(path = "Page.dataPreset", value = `{{Page.columnsData.datePreset.value != undefined}} ? Page.columnsData.datePreset.value : 'this_month'`) AFTER Steps.setdatePresetvalue.output
        productsFetching: _.productsFetching() /* fetching the active products from the leadzump  */
            output
                initialisingtheselectIndex: UIEngine.SetStore(path = `'Page.selectedAccIndex'`, value = -1) AFTER Steps.productsFetching.output
                    output
                        settingfilterfield: UIEngine.SetStore(path = "Page.storageFilter.field", value = "currently_in_use") AFTER Steps.initialisingtheselectIndex.output
                            output
                                assigningValuetofilterfield: UIEngine.SetStore(path = "Page.storageFilter.value", value = true) AFTER Steps.settingfilterfield.output
                                    output
                                        readPage: CoreServices.Storage.ReadPage(storageName = "MetaAdAccounts", size = 100, appCode = "marketingai", filter = Page.storageFilter) AFTER Steps.assigningValuetofilterfield.output
                                            output
                                                setStore3: UIEngine.SetStore(path = "Page.accounts", value = Steps.readPage.output.result.content)
                                                    output
                                                        setStore5: UIEngine.SetStore(path = "Page.accountSelected", value = Page.accounts[0]) AFTER Steps.setStore3.output
                                                            output
                                                                fetch_functionality: _.fetch_functionality() AFTER Steps.settingSearchParameter.output, Steps.initialisingThePageCursor.output, Steps.initialisingItemsPerPage.output, Steps.settingDatePreset.output, Steps.setStore5.output
                                                                    output
                                                                        settingConditiontocloseanimigrid: UIEngine.SetStore(path = "Page.showNormalGrids", value = "normalGrids") AFTER Steps.fetch_functionality.output
                                                                            output
                                                                                if: System.If(condition = Page.campaigns.length) AFTER Steps.settingConditiontocloseanimigrid.output
                                                                                    true
                                                                                        settingPageNumber: UIEngine.SetStore(path = "Page.currentPageNumber", value = 1) AFTER Steps.if.true
                                                                                            output
                                                                                                settingConditionFornonemptyCampaigns: UIEngine.SetStore(path = `'Page.showCampaigns'`, value = `'showCampaigns'`) AFTER Steps.settingPageNumber.output
                                                                                                    output
                                                                                                        forShowingTheCampaignsList: UIEngine.SetStore(path = "Page.listCampaigns", value = `'listing'`) AFTER Steps.settingConditionFornonemptyCampaigns.output
                                                                                    false
                                                                                        settingConditionForemptyCampaign: UIEngine.SetStore(path = `'Page.showCampaigns'`, value = `'noCampaigns'`) AFTER Steps.if.false