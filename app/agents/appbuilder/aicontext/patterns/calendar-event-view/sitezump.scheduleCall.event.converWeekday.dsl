FUNCTION converWeekday
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.DaySuffixArray", value = ["01st", "02nd", "03rd", "04th", "05th", "06th", "07th", "08th", "09th", "10th", "11th", "12th", "13th", "14th", "15th", "16th", "17th", "18th", "19th", "20th", "21st", "22nd", "23rd", "24th", "25th", "26th", "27th", "28th", "29th", "30th", "31st"])
        if: System.If(condition = Page.bookAcallDetails.date)
            true
                split: System.String.Split(string = Page.bookAcallDetails.date, searchString = `'/'`) AFTER Steps.if.true
                    output
                        setStore4: UIEngine.SetStore(path = "Page.DateMonthYear", value = Steps.split.output.result)
                            output
                                getDayOfWeek: System.Date.GetDayOfWeek(isoTimeStamp = `'{{Page.DateMonthYear[2]}}-{{Page.DateMonthYear[1]}}-{{Page.DateMonthYear[0]}}T01:00:00.000+05:30'`) AFTER Steps.setStore4.output
                                    output
                                        setStore2: UIEngine.SetStore(path = "Page.weekDay", value = Steps.getDayOfWeek.output.result)
                                            output
                                                setStore99: UIEngine.SetStore(path = "Page.weeksDayName", value = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]) AFTER Steps.setStore2.output
                                                    output
                                                        setStore0000: UIEngine.SetStore(path = "Page.DayFullName", value = Page.weeksDayName[{{Page.weekDay-1}}]) AFTER Steps.setStore99.output
                                setStore3: UIEngine.SetStore(path = "Page.DaySuffix", value = Page.DaySuffixArray[{{Page.DateMonthYear[0]-1}}]) AFTER Steps.setStore.output, Steps.setStore4.output
                                setStore6: UIEngine.SetStore(path = "Page.monthThreeChar", value = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]) AFTER Steps.setStore4.output
                                    output
                                        setStore7: UIEngine.SetStore(path = "Page.monthName", value = Page.monthThreeChar[{{Page.DateMonthYear[1]-1}}]) AFTER Steps.setStore6.output, Steps.setStore4.output