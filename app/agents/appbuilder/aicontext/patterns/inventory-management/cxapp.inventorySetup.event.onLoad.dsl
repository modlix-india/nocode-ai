FUNCTION onLoad
    LOGIC
        setStore2: UIEngine.SetStore(path = "Page.bookingStatus", value = ["Active", "Booked", "Blocked"])
            output
                setStore3: UIEngine.SetStore(path = "Page.unitType", value = ["Individual unit", "Duplex"]) AFTER Steps.setStore2.output
                    output
                        setStore4: UIEngine.SetStore(path = "Page.facing", value = ["East", "West", "North", "South"]) AFTER Steps.setStore3.output
                            output
                                setStore5: UIEngine.SetStore(path = "Page.removedFaces", value = ["South"]) AFTER Steps.setStore4.output
                                    output
                                        setStore6: UIEngine.SetStore(path = "Page.unitConfiguration", value = [{
    "bhkType": "1BHK",
    "toiletType": "1T"
}]) AFTER Steps.setStore5.output
                                            output
                                                setStore7: UIEngine.SetStore(path = "Page.bkkArray", value = ["1BHK", "2BHK", "3BHK", "4BHK", "5BHK"]) AFTER Steps.setStore6.output
                                                    output
                                                        setStore8: UIEngine.SetStore(path = "Page.tArray", value = ["1T", "2T", "3T", "4T", "5T"]) AFTER Steps.setStore7.output
                                                            output
                                                                selectedAndRemainingArraysofT: _.selectedAndRemainingArraysofT() AFTER Steps.setStore8.output
                                                                    output
                                                                        setStore9: UIEngine.SetStore(path = "Page.tAllTypeArray", value = ["1T", "2T", "3T", "4T", "5T"]) AFTER Steps.selectedAndRemainingArraysofT.output
                                                                            output
                                                                                setStore10: UIEngine.SetStore(path = "Page.bkkAllArray", value = ["1BHK", "2BHK", "3BHK", "4BHK", "5BHK"]) AFTER Steps.setStore9.output
                                                                                    output
                                                                                        setStore11: UIEngine.SetStore(path = "Page.plcConfigure", value = ["PLC east"]) AFTER Steps.setStore10.output
                                                                                            output
                                                                                                setStore12: UIEngine.SetStore(path = "Page.previousBHKType", value = "1BHK") AFTER Steps.setStore11.output
                                                                                                    output
                                                                                                        setStore14: UIEngine.SetStore(path = "Page.prevoiusToiletType", value = "1T") AFTER Steps.setStore12.output
                                                                                                            output
                                                                                                                setStore13: UIEngine.SetStore(path = "Page.removedPlcConfigure", value = ["PLC east", "PLC west", "PLC north", "PLC corner", "PLC club house facing", "PLC lake facing"]) AFTER Steps.setStore14.output
                                                                                                                    output
                                                                                                                        setStore15: UIEngine.SetStore(path = "Page.previousIndex", value = 0) AFTER Steps.setStore13.output
                                                                                                                            output
                                                                                                                                setStore: UIEngine.SetStore(path = `'Page.projectId'`, value = Url.pathParts[3]) AFTER Steps.setStore15.output
                                                                                                                                    output
                                                                                                                                        fetchingAttributesData: _.fetchingAttributesData() AFTER Steps.setStore.output
                                                                                                                                            output
                                                                                                                                                setStore1: UIEngine.SetStore(path = "Page.previousFacing", value = "South") AFTER Steps.fetchingAttributesData.output
        setStore19: UIEngine.SetStore(path = "Page.plchoverIndex", value = "")
            output
                setStore20: UIEngine.SetStore(path = "Page.unithoverIndex", value = "") AFTER Steps.setStore19.output
                    output
                        setStore21: UIEngine.SetStore(path = "Page.facinghoverIndex", value = "") AFTER Steps.setStore20.output
                            output
                                setStore22: UIEngine.SetStore(path = "Page.unitConfigergridHover", value = false) AFTER Steps.setStore21.output
                                    output
                                        setStore16: UIEngine.SetStore(path = "Page.PreviousIndex_facing", value = 0) AFTER Steps.setStore22.output
                                            output
                                                setStore17: UIEngine.SetStore(path = "Page.PreviousIndex_plc", value = 0) AFTER Steps.setStore16.output
                                                    output
                                                        setStore18: UIEngine.SetStore(path = "Page.previousPLC", value = "PLC east") AFTER Steps.setStore17.output
                                                            output
                                                                setStore23: UIEngine.SetStore(path = "Page.suggestMessage", value = false) AFTER Steps.setStore18.output