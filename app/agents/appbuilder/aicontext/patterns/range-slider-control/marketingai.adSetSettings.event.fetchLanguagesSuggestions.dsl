FUNCTION fetchLanguagesSuggestions
    LOGIC
        if: System.If(condition = `Page.languageValue != '' or Page.languageValue != undefined`)
            true
                metaLanguagesFetch: MarketingAI.MetaLanguagesFetch(limit = `''`, searchParameter = Page.languageValue, searchObjectType = `'adlocale'`) AFTER Steps.if.true
                    error
                        setStore1: UIEngine.SetStore(path = "Page.fetched_languages_error", value = Steps.metaLanguagesFetch.error.message)
                    output
                        setStore: UIEngine.SetStore(path = "Page.languages_suggestions", value = Steps.metaLanguagesFetch.output.languages)
                            output
                                setStore2: UIEngine.SetStore(path = "Page.showSuggestionsGrid.languageSuggestions", value = `'true'`) AFTER Steps.setStore.output