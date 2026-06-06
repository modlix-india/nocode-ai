FUNCTION dateFormate
    LOGIC
        if: System.If(condition = Page.campaignDetails.results)
            true
                toDateString: System.Date.ToDateString(isoTimeStamp = Page.campaignDetails.results[0].campaign.startDate, format = "dd/MM/yyyy") AFTER Steps.if.true
                    output
                        startDateFormate: UIEngine.SetStore(value = Steps.toDateString.output.result, path = "Page.startDate")
                            output
                                toDateString1: System.Date.ToDateString(isoTimeStamp = Page.campaignDetails.results[0].campaign.endDate, format = "dd/MM/yyyy") AFTER Steps.startDateFormate.output
                                    output
                                        endDateFormate: UIEngine.SetStore(value = Steps.toDateString1.output.result, path = "Page.endDate")