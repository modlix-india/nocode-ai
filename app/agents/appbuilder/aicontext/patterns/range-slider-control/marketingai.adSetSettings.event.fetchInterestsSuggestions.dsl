FUNCTION fetchInterestsSuggestions
    LOGIC
        if: System.If(condition = `Page.InterestSearchString != "" or Page.InterestSearchString != undefined`)
            true
                metaTargetingSearch: MarketingAI.MetaTargetingSearch(searchParameter = `Page.InterestSearchString ? Page.InterestSearchString : ''`, searchObjectType = "adinterest", limit = "5000") AFTER Steps.if.true
                    error
                        setStore1: UIEngine.SetStore(path = "Page.interst_fetchData_error", value = Steps.metaTargetingSearch.error.message)
                    output
                        suggestionsData: UIEngine.SetStore(path = "Page.fetchedData", value = Steps.metaTargetingSearch.output.searchResults.data) /* Fetched_interests_data
 */
                            output
                                setStore: UIEngine.SetStore(path = "Page.searchedArray", value = Page.fetchedData) AFTER Steps.suggestionsData.output /* assigning_fetchedData_tosearchedArray_for_searching */
                                    output
                                        setStore2: UIEngine.SetStore(path = "Page.showSuggestionsGrid.InterestsSuggestions", value = `'true'`) AFTER Steps.setStore.output