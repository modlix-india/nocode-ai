FUNCTION updateCampaignToAdDetails
    LOGIC
        updationOfCamapignBudget: _.updationOfCamapignBudget()
            output
                updationOfCampaignDetails: _.updationOfCampaignDetails() AFTER Steps.updationOfCamapignBudget.output
                    output
                        if2: System.If(condition = Page.removeLocationOperation.length or Page.updateLocationOperations.length) AFTER Steps.updationOfCampaignDetails.output
                            true
                                updationOfLocation: _.updationOfLocation() AFTER Steps.if2.true
                            output
                                if1: System.If(condition = Page.createArray.length or Page.removeArray.length) AFTER Steps.if2.output
                                    true
                                        updationOfAge: _.updationOfAge() AFTER Steps.if1.true
                                    output
                                        if1_Copy_1: System.If(condition = Page.createGenderArray.length or Page.removeGenderArray.length) AFTER Steps.if1.output
                                            true
                                                updationOfGender: _.updationOfGender() AFTER Steps.if1_Copy_1.true
                                            output
                                                if3: System.If(condition = Page.keywordRemoveOperation.length or Page.updatingKeywordObject.length) AFTER Steps.if1_Copy_1.output
                                                    true
                                                        updationOfKeywords: _.updationOfKeywords() AFTER Steps.if3.true
                                                    output
                                                        updationOfAd: _.updationOfAd() AFTER Steps.if3.output
                                                            output
                                                                payloadForCreatedSiteLinks: _.payloadForCreatedSiteLinks() AFTER Steps.updationOfAd.output
                                                                    output
                                                                        if4: System.If(condition = Page.updatedSiteLinks.length) AFTER Steps.payloadForCreatedSiteLinks.output
                                                                            true
                                                                                payloadForUpdateSiteLinks: _.payloadForUpdateSiteLinks() AFTER Steps.if4.true
                                                                            output
                                                                                postingDeletePayload: _.postingDeletePayload() AFTER Steps.if4.output
                                                                                    output
                                                                                        payloadForCreatedCallouts: _.payloadForCreatedCallouts() AFTER Steps.postingDeletePayload.output /* Creating_new_callout_assets */
                                                                                            output
                                                                                                if5: System.If(condition = Page.isCalloutsUpdated) AFTER Steps.payloadForCreatedCallouts.output /* Checking_any_callouts_updated */
                                                                                                    true
                                                                                                        payloadForUpdateCallouts: _.payloadForUpdateCallouts() AFTER Steps.if5.true
                                                                                                    output
                                                                                                        if7: System.If(condition = Page.deletedCallouts.length) AFTER Steps.if5.output /* Checking_any_callouts_assets_deleted */
                                                                                                            true
                                                                                                                postingDeleteCalloutsPayload: _.postingDeleteCalloutsPayload() AFTER Steps.if7.true
                                                                                                            output
                                                                                                                payloadForCreateCallAsset: _.payloadForCreateCallAsset() AFTER Steps.if7.output
                                                                                                                    output
                                                                                                                        if: System.If(condition = Page.isCallAssetUpdated) AFTER Steps.payloadForCreateCallAsset.output
                                                                                                                            true
                                                                                                                                payloadForUpdateCallAsset: _.payloadForUpdateCallAsset() AFTER Steps.if.true
                                                                                                                            output
                                                                                                                                if6: System.If(condition = Page.callAssetDeleteArray.length) AFTER Steps.if.output
                                                                                                                                    true
                                                                                                                                        postingDeleteCallAssetPayload: _.postingDeleteCallAssetPayload() AFTER Steps.if6.true
                                                                                                                                    output
                                                                                                                                        payloadForDeleteSnippet: _.payloadForDeleteSnippet() AFTER Steps.if6.output
                                                                                                                                            output
                                                                                                                                                payloadForCreateSnippets: _.payloadForCreateSnippets() AFTER Steps.payloadForDeleteSnippet.output
                                                                                                                                                    output
                                                                                                                                                        navigate: UIEngine.Navigate(linkPath = "/googleCampaign") AFTER Steps.payloadForCreateSnippets.output