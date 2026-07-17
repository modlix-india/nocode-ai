FUNCTION onLoad
    LOGIC
        setStore27: UIEngine.SetStore(path = "Page.showAnimation", value = true)
            output
                customerId: UIEngine.SetStore(path = "Page.customerId", value = Store.urlDetails.queryParameters.customerId) AFTER Steps.setStore27.output
                    output
                        setStore33: UIEngine.SetStore(path = "Page.loginCustomerId", value = Store.urlDetails.queryParameters.loginCustomerId) AFTER Steps.customerId.output
                            output
                                setStore37: UIEngine.SetStore(path = "Page.duration ", value = "LAST_30_DAYS") AFTER Steps.setStore33.output
                                    output
                                        setStore25: UIEngine.SetStore(path = "Page.showKeywords", value = true) AFTER Steps.setStore37.output
                                            output
                                                NotingCampaignIdFromAdsetRedirecting: UIEngine.SetStore(path = "Page.campaignId", value = Store.urlDetails.queryParameters.campaignId) AFTER Steps.setStore25.output
                                                    output
                                                        budgetAndCampaginDataFetching: _.budgetAndCampaginDataFetching() AFTER Steps.NotingCampaignIdFromAdsetRedirecting.output
                                                            output
                                                                if_Copy_1: System.If(condition = Page.existingGeoLocations.results.length) AFTER Steps.budgetAndCampaginDataFetching.output
                                                                    false
                                                                        setStore_Copy_3: UIEngine.SetStore(path = "Page.existingGeoLocations.results", value = []) AFTER Steps.if_Copy_1.false
                                                                    output
                                                                        dateFormate: _.dateFormate() AFTER Steps.if_Copy_1.output
                                                                            output
                                                                                adGroupDataFetching: _.adGroupDataFetching() AFTER Steps.dateFormate.output
                                                                                    output
                                                                                        adDataFetching: _.adDataFetching() AFTER Steps.adGroupDataFetching.output
                                                                                            output
                                                                                                fetchingSiteLinksDetails: _.fetchingSiteLinksDetails() AFTER Steps.adDataFetching.output
                                                                                                    output
                                                                                                        fetchCallAssetDetails: _.fetchCallAssetDetails() AFTER Steps.fetchingSiteLinksDetails.output
                                                                                                            output
                                                                                                                setStore35: UIEngine.SetStore(path = "Page.ctr", value = Page.campaignDetails.results[0].metrics.ctr ?? 0 * 100) AFTER Steps.fetchCallAssetDetails.output
                                                                                                                    output
                                                                                                                        round1: System.Math.Round(value = Page.campaignDetails.results[0].metrics.averageCpc ?? 0 ) AFTER Steps.setStore35.output
                                                                                                                            output
                                                                                                                                setStore36: UIEngine.SetStore(path = "Page.cpc", value = Steps.round1.output.value)
                                                                                                                                    output
                                                                                                                                        setStore39: UIEngine.SetStore(path = "Page.costMicros", value = {{Page.campaignDetails.results[0].metrics.costMicros}} / 1000000) AFTER Steps.setStore36.output
                                                                                                                                            output
                                                                                                                                                if1: System.If(condition = Page.existingSiteLinkDetails.results) AFTER Steps.setStore39.output
                                                                                                                                                    false
                                                                                                                                                        setStore34: UIEngine.SetStore(path = "Page.existingSiteLinkDetails.results", value = []) AFTER Steps.if1.false
                                                                                                                                                    output
                                                                                                                                                        setStore27_Copy_1: UIEngine.SetStore(path = "Page.showAnimation", value = false) AFTER Steps.if1.output
                                                                                                                                                            output
                                                                                                                                                                setStore1: UIEngine.SetStore(path = "Page.campaignType", value = `Page.campaignDetails.results[0].campaign.advertisingChannelType + ' CAMPAIGN'`) AFTER Steps.setStore27_Copy_1.output
                                                                                                                                                                    output
                                                                                                                                                                        setStore: UIEngine.SetStore(path = "Page.campaignName", value = Page.campaignDetails.results[0].campaign.name) AFTER Steps.setStore1.output
                                                                                                                                                                            output
                                                                                                                                                                                setStore3: UIEngine.SetStore(path = "Page.dailyBudget", value = {{Page.campaignDetails.results[0].campaignBudget.amountMicros ?? 0}} / 1000000) AFTER Steps.setStore.output
                                                                                                                                                                                    output
                                                                                                                                                                                        setStore2: UIEngine.SetStore(path = "Page.adGroupName", value = Page.adGroupDetails.results[0].adGroup.name) AFTER Steps.setStore3.output
                                                                                                                                                                                            output
                                                                                                                                                                                                setStore5: UIEngine.SetStore(path = "Page.adStatus", value = Page.adDetails.results[0].adGroupAd.status) AFTER Steps.setStore2.output
                                                                                                                                                                                                    output
                                                                                                                                                                                                        setStore3_Copy_1: UIEngine.SetStore(path = "Page.selectedLocations", value = []) AFTER Steps.setStore5.output
                                                                                                                                                                                                            output
                                                                                                                                                                                                                setStore19: UIEngine.SetStore(path = "Page.adName", value = Page.adDetails.results[0].adGroupAd.ad.name) AFTER Steps.setStore3_Copy_1.output
                                                                                                                                                                                                                    output
                                                                                                                                                                                                                        setStore6: UIEngine.SetStore(path = "Page.geoConstantsIds", value = []) AFTER Steps.setStore19.output
                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                setStore20: UIEngine.SetStore(path = "Page.SearchTerms", value = {
    "results": [
        {
            "metrics": {
                "clicks": "545",
                "conversions": 18.5,
                "costMicros": "<PHONE>",
                "ctr": 0.17208714872118724,
                "averageCpc": 97953999.1706422,
                "impressions": "3167"
            },
            "searchTermView": {
                "resourceName": "customers/<PHONE>/searchTermViews/<PHONE>~<PHONE>~dmFsbWFyayBjaXR5dmlsbGU",
                "status": "ADDED",
                "searchTerm": "valmark cityville",
                "adGroup": "customers/<PHONE>/adGroups/<PHONE>"
            },
            "segments": {
                "searchTermMatchType": "EXACT",
                "keyword": {
                    "info": {
                        "text": "valmark cityville"
                    },
                    "adGroupCriterion": "customers/<PHONE>/adGroupCriteria/<PHONE>~<PHONE>"
                }
            }
        },
        {
            "metrics": {
                "clicks": "0",
                "conversions": 0,
                "costMicros": "0",
                "ctr": 0,
                "impressions": "1"
            },
            "searchTermView": {
                "resourceName": "customers/<PHONE>/searchTermViews/<PHONE>~<PHONE>~dmFsbWFyayBjaXR5dmlsbGUgYWRkcmVzcw",
                "status": "ADDED",
                "searchTerm": "valmark cityville address",
                "adGroup": "customers/<PHONE>/adGroups/<PHONE>"
            },
            "segments": {
                "searchTermMatchType": "PHRASE",
                "keyword": {
                    "info": {
                        "text": "valmark cityville"
                    },
                    "adGroupCriterion": "customers/<PHONE>/adGroupCriteria/<PHONE>~<PHONE>"
                }
            }
        },
        {
            "metrics": {
                "clicks": "0",
                "conversions": 0,
                "costMicros": "0",
                "ctr": 0,
                "impressions": "1"
            },
            "searchTermView": {
                "resourceName": "customers/<PHONE>/searchTermViews/<PHONE>~<PHONE>~dmFsbWFyayBjaXR5dmlsbGUgYmxvY2sgOA",
                "status": "ADDED",
                "searchTerm": "valmark cityville block 8",
                "adGroup": "customers/<PHONE>/adGroups/<PHONE>"
            },
            "segments": {
                "searchTermMatchType": "PHRASE",
                "keyword": {
                    "info": {
                        "text": "valmark cityville"
                    },
                    "adGroupCriterion": "customers/<PHONE>/adGroupCriteria/<PHONE>~<PHONE>"
                }
            }
        },
        {
            "metrics": {
                "clicks": "0",
                "conversions": 0,
                "costMicros": "0",
                "ctr": 0,
                "impressions": "2"
            },
            "searchTermView": {
                "resourceName": "customers/<PHONE>/searchTermViews/<PHONE>~<PHONE>~d2FsbG1hcmsgY2l0eSB2aWxsZQ",
                "status": "ADDED",
                "searchTerm": "wallmark city ville",
                "adGroup": "customers/<PHONE>/adGroups/<PHONE>"
            },
            "segments": {
                "searchTermMatchType": "NEAR_EXACT",
                "keyword": {
                    "info": {
                        "text": "valmark cityville"
                    },
                    "adGroupCriterion": "customers/<PHONE>/adGroupCriteria/<PHONE>~<PHONE>"
                }
            }
        }
    ]
}) AFTER Steps.setStore6.output
                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                        setStore_Copy_2: UIEngine.SetStore(path = "Page.selectedKeywordArray", value = []) AFTER Steps.setStore20.output
                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                setStore29: UIEngine.SetStore(path = "Page.lengthOfExistingSiteLinks", value = Page.existingSiteLinkDetails.results.length) AFTER Steps.setStore_Copy_2.output
                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                        setStore32: UIEngine.SetStore(path = "Page.lengthOfCurrentSiteLinks ", value = 0) AFTER Steps.setStore29.output
                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                setStore30: UIEngine.SetStore(path = "Page.siteLinkDeleteArray", value = []) AFTER Steps.setStore32.output
                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                        existingAgeAndGender: _.existingAgeAndGender() AFTER Steps.setStore30.output
                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                setStore13: UIEngine.SetStore(path = "Page.removeAgeOperation", value = []) AFTER Steps.existingAgeAndGender.output
                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                        setStore12: UIEngine.SetStore(path = "Page.removeLocationOperation", value = []) AFTER Steps.setStore13.output
                                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                                setStore_Copy_1: UIEngine.SetStore(path = "Page.updateLocationOperations", value = []) AFTER Steps.setStore12.output
                                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                                        setStore11: UIEngine.SetStore(path = "Page.updateAgeOperation", value = []) AFTER Steps.setStore_Copy_1.output
                                                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                                                setStore9: UIEngine.SetStore(path = "Page.adGroupId", value = Page.adGroupDetails.results[0].adGroup.resourceName) AFTER Steps.setStore11.output
                                                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                                                        setStore10: UIEngine.SetStore(path = "Page.adId", value = Page.adDetails.results[0].adGroupAd.ad.resourceName) AFTER Steps.setStore9.output
                                                                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                                                                setStore14: UIEngine.SetStore(path = "Page.keywordRemoveOperation", value = []) AFTER Steps.setStore10.output
                                                                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                                                                        setStore15: UIEngine.SetStore(path = "Page.operations", value = []) AFTER Steps.setStore14.output
                                                                                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                                                                                setStore16: UIEngine.SetStore(path = "Page.updatingKeywordObject", value = []) AFTER Steps.setStore15.output
                                                                                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                                                                                        setStore21: UIEngine.SetStore(path = "Page.removeArray", value = []) AFTER Steps.setStore16.output
                                                                                                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                                                                                                setStore22: UIEngine.SetStore(path = "Page.createArray", value = []) AFTER Steps.setStore21.output
                                                                                                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                                                                                                        setStore23: UIEngine.SetStore(path = "Page.removeGenderArray", value = []) AFTER Steps.setStore22.output
                                                                                                                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                                                                                                                setStore24: UIEngine.SetStore(path = "Page.createGenderArray", value = []) AFTER Steps.setStore23.output
                                                                                                                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                                                                                                                        setStore26: UIEngine.SetStore(path = "Page.SearchTermsArray", value = []) AFTER Steps.setStore24.output
                                                                                                                                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                                                                                                                                setStore28: UIEngine.SetStore(path = "Page.createdSiteLinks", value = []) AFTER Steps.setStore26.output
                                                                                                                                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                                                                                                                                        setStore31: UIEngine.SetStore(path = "Page.updatedSiteLinks", value = []) AFTER Steps.setStore28.output
                                                                                                                                                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                                                                                                                                                setStore38: UIEngine.SetStore(path = "Page.callAssetDeleteArray", value = []) AFTER Steps.setStore31.output
                                                                                                                                                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                                                                                                                                                        setStore40: UIEngine.SetStore(path = "Page.createdCallAsset", value = []) AFTER Steps.setStore38.output
                                                                                                                                                                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                                                                                                                                                                setStore41: UIEngine.SetStore(path = "Page.updatedCallAsset", value = []) AFTER Steps.setStore40.output
                                                                                                                                                                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                                                                                                                                                                        if: System.If(condition = Page.adDetails.results) AFTER Steps.setStore41.output
                                                                                                                                                                                                                                                                                                                                                                                                                                            true
                                                                                                                                                                                                                                                                                                                                                                                                                                                setStore1_Copy_1: UIEngine.SetStore(path = "Page.finalUrls", value = Page.adDetails.results[0].adGroupAd.ad.finalUrls) AFTER Steps.if.true
                                                                                                                                                                                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                                                                                                                                                                                        setStore7: UIEngine.SetStore(path = "Page.updatingHeadlines", value = Page.adDetails.results[0].adGroupAd.ad.responsiveSearchAd.headlines) AFTER Steps.setStore1_Copy_1.output
                                                                                                                                                                                                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                                                                                                                                                                                                headlinesTextFiltering: _.headlinesTextFiltering() AFTER Steps.setStore7.output
                                                                                                                                                                                                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                                                                                                                                                                                                        setStore8: UIEngine.SetStore(path = "Page.updatingDescriptions", value = Page.adDetails.results[0].adGroupAd.ad.responsiveSearchAd.descriptions) AFTER Steps.headlinesTextFiltering.output
                                                                                                                                                                                                                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                descriptionTextFiltering: _.descriptionTextFiltering() AFTER Steps.setStore8.output
                                                                                                                                                                                                                                                                                                                                                                                                                                            false
                                                                                                                                                                                                                                                                                                                                                                                                                                                setStore4: UIEngine.SetStore(path = "Page.finalUrls", value = []) AFTER Steps.if.false
                                                                                                                                                                                                                                                                                                                                                                                                                                                    output
                                                                                                                                                                                                                                                                                                                                                                                                                                                        setStore17: UIEngine.SetStore(path = "Page.updatedDescription", value = []) AFTER Steps.setStore4.output
                                                                                                                                                                                                                                                                                                                                                                                                                                                            output
                                                                                                                                                                                                                                                                                                                                                                                                                                                                setStore18: UIEngine.SetStore(path = "Page.updatedHeadlines", value = []) AFTER Steps.setStore17.output