FUNCTION fetch_geo_locations_suggesstions
    LOGIC
        if: System.If(condition = `Page.locationValue  != ""`)
            true
                if1: System.If(condition = Page.allowed_countries != undefined) AFTER Steps.if.true
                    true
                        setStore4: UIEngine.SetStore(path = "Page.allowedCountriesArray", value = Page.allowed_countries) AFTER Steps.if1.true
                    false
                        setStore5: UIEngine.SetStore(path = "Page.allowedCountriesArray", value = []) AFTER Steps.if1.false
                    output
                        toString1: System.String.ToString(anytype = Page.allowedCountriesArray) AFTER Steps.if1.output
                            output
                                metaGeoLocationsSearch: MarketingAI.MetaGeoLocationsSearch(searchParameter = Page.locationValue, searchObjectType = "adgeolocation", allowedCountries = Steps.toString1.output.result, locationTypes = "[]")
                                    error
                                        setStore1: UIEngine.SetStore(path = "Page.error", value = Steps.metaGeoLocationsSearch.error.message)
                                    Output
                                        setStore: UIEngine.SetStore(path = "Page.locations", value = Steps.metaGeoLocationsSearch.Output.geoLocations)
                                            output
                                                setStore2: UIEngine.SetStore(path = `'Page.showlocationsgrid'`, value = `'true'`) AFTER Steps.setStore.output