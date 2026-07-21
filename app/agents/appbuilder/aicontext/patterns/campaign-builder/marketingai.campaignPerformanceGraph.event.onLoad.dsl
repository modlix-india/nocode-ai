FUNCTION onLoad
    LOGIC
        setStore3: UIEngine.SetStore(path = "Page.campaignData", value = {
    "date": [],
    "clicks": [],
    "conversions": [],
    "costMicros": [],
    "impressions": []
})
            output
                setStore: UIEngine.SetStore(path = "Page.performanceData", value = {
    "results": [
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "31",
                "conversions": 1,
                "costMicros": "<PHONE>",
                "impressions": "193"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "36",
                "conversions": 0,
                "costMicros": "<PHONE>",
                "impressions": "237"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "42",
                "conversions": 1.986147,
                "costMicros": "<PHONE>",
                "impressions": "263"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "39",
                "conversions": 3,
                "costMicros": "<PHONE>",
                "impressions": "242"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "35",
                "conversions": 2,
                "costMicros": "<PHONE>",
                "impressions": "238"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "35",
                "conversions": 2,
                "costMicros": "<PHONE>",
                "impressions": "257"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "41",
                "conversions": 3.5,
                "costMicros": "<PHONE>",
                "impressions": "221"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "41",
                "conversions": 5.5,
                "costMicros": "<PHONE>",
                "impressions": "243"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "49",
                "conversions": 1,
                "costMicros": "<PHONE>",
                "impressions": "251"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "41",
                "conversions": 2,
                "costMicros": "<PHONE>",
                "impressions": "225"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "37",
                "conversions": 0,
                "costMicros": "<PHONE>",
                "impressions": "244"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "52",
                "conversions": 0,
                "costMicros": "<PHONE>",
                "impressions": "303"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "26",
                "conversions": 0,
                "costMicros": "<PHONE>",
                "impressions": "172"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "34",
                "conversions": 2,
                "costMicros": "<PHONE>",
                "impressions": "191"
            },
            "segments": {
                "date": "<PHONE>"
            }
        },
        {
            "campaign": {
                "resourceName": "customers/<PHONE>/campaigns/<PHONE>"
            },
            "metrics": {
                "clicks": "21",
                "conversions": 0,
                "costMicros": "<PHONE>",
                "impressions": "188"
            },
            "segments": {
                "date": "<PHONE>"
            }
        }
    ],
    "fieldMask": "segments.date,metrics.costMicros,metrics.impressions,metrics.clicks,metrics.conversions",
    "queryResourceConsumption": "556"
}) AFTER Steps.setStore3.output
                    output
                        NotingCampaignIdFromAdsetRedirecting: UIEngine.SetStore(path = "Page.campaignId", value = Store.urlDetails.queryParameters.campaignId) AFTER Steps.setStore.output
                            output
                                setStore4_Copy_1: UIEngine.SetStore(path = "Page.customerId", value = Store.urlDetails.queryParameters.customerId) AFTER Steps.NotingCampaignIdFromAdsetRedirecting.output
                                    output
                                        setStore9: UIEngine.SetStore(path = "Page.loginCustomerId", value = Store.urlDetails.queryParameters.loginCustomerId) AFTER Steps.setStore4_Copy_1.output
                                            output
                                                setStore10: UIEngine.SetStore(path = "Page.duration", value = "LAST_30_DAYS") AFTER Steps.setStore9.output
                                                    output
                                                        setStore13: UIEngine.SetStore(path = "Page.durationValue", value = "Last 30 Days") AFTER Steps.setStore10.output
                                                            output
                                                                setStore11: UIEngine.SetStore(path = "Page.relativeDuration", value = [{
    "name": "TODAY",
    "value": "Today"
}, {
    "name": "LAST_7_DAYS",
    "value": "Last 7 Days"
}, {
    "name": "LAST_14_DAYS",
    "value": "Last 14 Days"
}, {
    "name": "LAST_30_DAYS",
    "value": "Last 30 Days"
}, {
    "name": "YESTERDAY",
    "value": "Yesterday"
}, {
    "name": "THIS_MONTH",
    "value": "This Month"
}, {
    "name": "LAST_30_DAYS",
    "value": "Last Month"
}]) AFTER Steps.setStore13.output
                                                                    output
                                                                        setStore12: UIEngine.SetStore(path = "Page.durationIndexing", value = -1) AFTER Steps.setStore11.output
                                                                            output
                                                                                forEachLoop: System.Loop.ForEachLoop(source = Page.performanceData.results) AFTER Steps.setStore12.output
                                                                                    iteration
                                                                                        if: System.If(condition = Page.performanceData.results[{{Steps.forEachLoop.iteration.index}}].segments)
                                                                                            true
                                                                                                insertLast: System.Array.InsertLast(element = Page.performanceData.results[{{Steps.forEachLoop.iteration.index}}].segments.date, source = Page.campaignData.date) AFTER Steps.if.true
                                                                                                    output
                                                                                                        setStore1: UIEngine.SetStore(path = "Page.campaignData.date", value = Steps.insertLast.output.result)
                                                                                        if1: System.If(condition = Page.performanceData.results[{{Steps.forEachLoop.iteration.index}}].metrics)
                                                                                            true
                                                                                                insertLast3: System.Array.InsertLast(element = Page.performanceData.results[{{Steps.forEachLoop.iteration.index}}].metrics.costMicros, source = Page.campaignData.costMicros) AFTER Steps.if1.true
                                                                                                    output
                                                                                                        setStore5: UIEngine.SetStore(path = "Page.campaignData.costMicros", value = Steps.insertLast3.output.result)
                                                                                                insertLast1: System.Array.InsertLast(element = Page.performanceData.results[{{Steps.forEachLoop.iteration.index}}].metrics.clicks, source = Page.campaignData.clicks) AFTER Steps.if1.true
                                                                                                    output
                                                                                                        setStore2: UIEngine.SetStore(path = "Page.campaignData.clicks", value = Steps.insertLast1.output.result)
                                                                                                insertLast2: System.Array.InsertLast(element = Page.performanceData.results[{{Steps.forEachLoop.iteration.index}}].metrics.conversions, source = Page.campaignData.conversions) AFTER Steps.if1.true
                                                                                                    output
                                                                                                        setStore4: UIEngine.SetStore(path = "Page.campaignData.conversions", value = Steps.insertLast2.output.result)
                                                                                                insertLast4: System.Array.InsertLast(element = {{Steps.forEachLoop.iteration.each.metrics.impressions}}, source = Page.campaignData.impressions) AFTER Steps.if1.true
                                                                                                    output
                                                                                                        setStore6: UIEngine.SetStore(path = "Page.campaignData.impressions", value = Steps.insertLast4.output.result)
                                                                                    output
                                                                                        setStore7: UIEngine.SetStore(path = `"Page.impressionsObj.date"`, value = Page.campaignData.date) AFTER Steps.forEachLoop.output
                                                                                            output
                                                                                                setStore8: UIEngine.SetStore(path = `"Page.impressionsObj.impressions"`, value = Page.campaignData.impressions) AFTER Steps.setStore7.output
                                                                                                    output
                                                                                                        campaignDetails: _.campaignDetails() AFTER Steps.setStore8.output
                                                                                                            output
                                                                                                                adGroupDataFetching: _.adGroupDataFetching() AFTER Steps.campaignDetails.output
                                                                                                                    output
                                                                                                                        budget_Recommendation: _.budget_Recommendation() AFTER Steps.adGroupDataFetching.output
                                                                                                                            output
                                                                                                                                searchTermOnload: _.searchTermOnload() AFTER Steps.budget_Recommendation.output
                                                                                                                                    output
                                                                                                                                        fetchingSearchTerms: _.fetchingSearchTerms() AFTER Steps.searchTermOnload.output