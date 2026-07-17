FUNCTION callSchedules
    LOGIC
        setStore7: UIEngine.SetStore(path = "Page.slotsArray", value = [])
        setStore21_Copy_1_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.scheduleCallDetails.phoneNumber", value = Store.auth.user.phoneNumber)
        setStore21_Copy_1: UIEngine.SetStore(path = "Page.scheduleCallDetails.lastName", value = Store.auth.user.lastName)
        setStore21_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.scheduleCallDetails.emailId", value = Store.auth.user.emailId)
        setStore2: UIEngine.SetStore(path = "Page.callDuration", value = {{Page.project.scheduleAcall.duration}})
        setStore21: UIEngine.SetStore(path = "Page.scheduleCallDetails.firstName", value = Store.auth.user.firstName)
        if4333: System.If(condition = `Page.project.scheduleAcall.startTime = "" and Page.project.scheduleAcall.endTime = ""`)
            true
                message: UIEngine.Message(msg = "Please Configure", type = "WARNING") AFTER Steps.if4333.true
            false
                setStore: UIEngine.SetStore(path = "Page.initialstartTime", value = Page.project.scheduleAcall.startTime) AFTER Steps.if4333.false
                    output
                        split: System.String.Split(string = Page.initialstartTime, searchString = `':'`) AFTER Steps.setStore.output
                            output
                                setStore1: UIEngine.SetStore(path = "Page.startHour", value = Steps.split.output.result[0])
                                    output
                                        setStore4: UIEngine.SetStore(path = "Page.totalStartMinutes", value = {{Page.startHour}} * 60 + {{Page.startMinute}}) AFTER Steps.setStore1.output, Steps.setStore3.output
                                            output
                                                setStore5: UIEngine.SetStore(path = "Page.totalTime", value = {{Page.totalEndMinutes}} - {{Page.totalStartMinutes}}) AFTER Steps.setStore4.output, Steps.setStore4_Copy_1.output
                                                    output
                                                        floor: System.Math.Floor(value = {{Page.totalTime}} / {{Page.callDuration}}) AFTER Steps.setStore5.output, Steps.setStore2.output
                                                            output
                                                                setStore6: UIEngine.SetStore(path = "Page.noOfSlots", value = Steps.floor.output.value) AFTER Steps.floor.output
                                                                    output
                                                                        rangeLoop: System.Loop.RangeLoop(to = Page.noOfSlots) AFTER Steps.setStore6.output, Steps.setStore7.output
                                                                            iteration
                                                                                setStore8: UIEngine.SetStore(path = "Page.index", value = Steps.rangeLoop.iteration.index) AFTER Steps.rangeLoop.iteration
                                                                                    output
                                                                                        setStore10: UIEngine.SetStore(path = "Page.indexAndDur", value = {{Page.callDuration}} * {{Page.index}}) AFTER Steps.setStore8.output, Steps.setStore2.output, Steps.rangeLoop.iteration
                                                                                            output
                                                                                                setStore9: UIEngine.SetStore(path = "Page.currentStartMinutes", value = {{Page.totalStartMinutes}}  + {{Page.indexAndDur}}) AFTER Steps.setStore10.output, Steps.setStore4.output, Steps.rangeLoop.iteration
                                                                                                    output
                                                                                                        floor1: System.Math.Floor(value = Page.currentStartMinutes / 60) AFTER Steps.rangeLoop.iteration, Steps.setStore9.output
                                                                                                            output
                                                                                                                setStore11: UIEngine.SetStore(path = "Page.slotStartHour", value = Steps.floor1.output.value) AFTER Steps.rangeLoop.iteration, Steps.floor1.output
                                                                                                                    output
                                                                                                                        if: System.If(condition = Page.slotStartHour  < 10) AFTER Steps.setStore11.output
                                                                                                                            true
                                                                                                                                setStore15: UIEngine.SetStore(path = `'Page.slotStartHour'`, value = `'0{{Page.slotStartHour}}'`) AFTER Steps.if.true, Steps.setStore11.output, Steps.rangeLoop.iteration
                                                                                                                            output
                                                                                                                                setStore19: UIEngine.SetStore(path = `'Page.slotsArray[{{Steps.rangeLoop.iteration.index}}].slotStartTime'`, value = `'{{Page.slotStartHour}}:{{Page.slotStartMin}}'`) AFTER Steps.if.output, Steps.if1.output
                                                                                                        setStore12: UIEngine.SetStore(path = "Page.slotStartMin", value = {{Page.currentStartMinutes}} % 60) AFTER Steps.rangeLoop.iteration, Steps.setStore9.output
                                                                                                            output
                                                                                                                if1: System.If(condition = Page.slotStartMin < 10) AFTER Steps.setStore12.output
                                                                                                                    true
                                                                                                                        setStore16: UIEngine.SetStore(path = `'Page.slotStartMin'`, value = `'0{{Page.slotStartMin}}'`) AFTER Steps.setStore12.output, Steps.if1.true
                                                                                                        setStore9_Copy_1: UIEngine.SetStore(path = "Page.currentEndMinutes", value = {{Page.currentStartMinutes}}  + {{Page.callDuration}}) AFTER Steps.setStore10.output, Steps.setStore4.output, Steps.setStore2.output, Steps.rangeLoop.iteration, Steps.setStore9.output
                                                                                                            output
                                                                                                                floor3: System.Math.Floor(value = Page.currentEndMinutes / 60) AFTER Steps.setStore9_Copy_1.output, Steps.rangeLoop.iteration
                                                                                                                    output
                                                                                                                        setStore13: UIEngine.SetStore(path = "Page.slotEndHour", value = Steps.floor3.output.value) AFTER Steps.floor3.output, Steps.rangeLoop.iteration
                                                                                                                            output
                                                                                                                                if2: System.If(condition = Page.slotEndHour < 10) AFTER Steps.setStore13.output
                                                                                                                                    true
                                                                                                                                        setStore17: UIEngine.SetStore(path = `'Page.slotEndHour'`, value = `'0{{Page.slotEndHour}}'`) AFTER Steps.if2.true, Steps.setStore13.output
                                                                                                                                    output
                                                                                                                                        setStore19_Copy_1: UIEngine.SetStore(path = `'Page.slotsArray[{{Steps.rangeLoop.iteration.index}}].slotEndTime'`, value = `'{{Page.slotEndHour}}:{{Page.slotEndMin}}'`) AFTER Steps.if2.output, Steps.if3.output
                                                                                                                setStore14: UIEngine.SetStore(path = "Page.slotEndMin", value = {{Page.currentEndMinutes}} % 60) AFTER Steps.setStore9_Copy_1.output, Steps.rangeLoop.iteration
                                                                                                                    output
                                                                                                                        if3: System.If(condition = Page.slotEndMin < 10) AFTER Steps.setStore14.output
                                                                                                                            true
                                                                                                                                setStore18: UIEngine.SetStore(path = `'Page.slotEndMin'`, value = `'0{{Page.slotEndMin}}'`) AFTER Steps.if3.true, Steps.setStore14.output
                                setStore3: UIEngine.SetStore(value = Steps.split.output.result[1], path = "Page.startMinute")
                setStore_Copy_1: UIEngine.SetStore(path = "Page.initialEndTime", value = Page.project.scheduleAcall.endTime) AFTER Steps.if4333.false
                    output
                        split2: System.String.Split(string = Page.initialEndTime, searchString = `':'`) AFTER Steps.setStore_Copy_1.output
                            output
                                setStore1_Copy_1: UIEngine.SetStore(path = "Page.endHour", value = Steps.split2.output.result[0])
                                    output
                                        setStore4_Copy_1: UIEngine.SetStore(path = "Page.totalEndMinutes", value = {{Page.endHour}} * 60 + {{Page.endMinute}}) AFTER Steps.setStore1_Copy_1.output, Steps.setStore3_Copy_1.output
                                setStore3_Copy_1: UIEngine.SetStore(value = Steps.split2.output.result[1], path = "Page.endMinute") AFTER Steps.split2.output.result
        setStore5_Copy_1: UIEngine.SetStore(path = "Page.slotsArray", value = [{
    "slotStartTime": "12:00 AM",
    "slotEndTime": "12:15 AM"
}, {
    "slotStartTime": "12:15 AM",
    "slotEndTime": "12:30 AM"
}, {
    "slotStartTime": "12:30 AM",
    "slotEndTime": "12:45 AM"
}, {
    "slotStartTime": "12:45 AM",
    "slotEndTime": "01:00 AM"
}, {
    "slotStartTime": "01:00 AM",
    "slotEndTime": "01:15 AM"
}, {
    "slotStartTime": "01:15 AM",
    "slotEndTime": "01:30 AM"
}, {
    "slotStartTime": "01:30 AM",
    "slotEndTime": "01:45 AM"
}, {
    "slotStartTime": "01:45 AM",
    "slotEndTime": "02:00 AM"
}, {
    "slotStartTime": "02:00 AM",
    "slotEndTime": "02:15 AM"
}, {
    "slotStartTime": "02:15 AM",
    "slotEndTime": "02:30 AM"
}, {
    "slotStartTime": "02:30 AM",
    "slotEndTime": "02:45 AM"
}, {
    "slotStartTime": "02:45 AM",
    "slotEndTime": "03:00 AM"
}, {
    "slotStartTime": "03:00 AM",
    "slotEndTime": "03:15 AM"
}, {
    "slotStartTime": "03:15 AM",
    "slotEndTime": "03:30 AM"
}, {
    "slotStartTime": "03:30 AM",
    "slotEndTime": "03:45 AM"
}, {
    "slotStartTime": "03:45 AM",
    "slotEndTime": "04:00 AM"
}, {
    "slotStartTime": "04:00 AM",
    "slotEndTime": "04:15 AM"
}, {
    "slotStartTime": "04:15 AM",
    "slotEndTime": "04:30 AM"
}, {
    "slotStartTime": "04:30 AM",
    "slotEndTime": "04:45 AM"
}, {
    "slotStartTime": "04:45 AM",
    "slotEndTime": "05:00 AM"
}, {
    "slotStartTime": "05:00 AM",
    "slotEndTime": "05:15 AM"
}, {
    "slotStartTime": "05:15 AM",
    "slotEndTime": "05:30 AM"
}, {
    "slotStartTime": "05:30 AM",
    "slotEndTime": "05:45 AM"
}, {
    "slotStartTime": "05:45 AM",
    "slotEndTime": "06:00 AM"
}, {
    "slotStartTime": "06:00 AM",
    "slotEndTime": "06:15 AM"
}, {
    "slotStartTime": "06:15 AM",
    "slotEndTime": "06:30 AM"
}, {
    "slotStartTime": "06:30 AM",
    "slotEndTime": "06:45 AM"
}, {
    "slotStartTime": "06:45 AM",
    "slotEndTime": "07:00 AM"
}, {
    "slotStartTime": "07:00 AM",
    "slotEndTime": "07:15 AM"
}, {
    "slotStartTime": "07:15 AM",
    "slotEndTime": "07:30 AM"
}, {
    "slotStartTime": "07:30 AM",
    "slotEndTime": "07:45 AM"
}, {
    "slotStartTime": "07:45 AM",
    "slotEndTime": "08:00 AM"
}, {
    "slotStartTime": "08:00 AM",
    "slotEndTime": "08:15 AM"
}, {
    "slotStartTime": "08:15 AM",
    "slotEndTime": "08:30 AM"
}, {
    "slotStartTime": "08:30 AM",
    "slotEndTime": "08:45 AM"
}, {
    "slotStartTime": "08:45 AM",
    "slotEndTime": "09:00 AM"
}, {
    "slotStartTime": "09:00 AM",
    "slotEndTime": "09:15 AM"
}, {
    "slotStartTime": "09:15 AM",
    "slotEndTime": "09:30 AM"
}, {
    "slotStartTime": "09:30 AM",
    "slotEndTime": "09:45 AM"
}, {
    "slotStartTime": "09:45 AM",
    "slotEndTime": "10:00 AM"
}, {
    "slotStartTime": "10:00 AM",
    "slotEndTime": "10:15 AM"
}, {
    "slotStartTime": "10:15 AM",
    "slotEndTime": "10:30 AM"
}, {
    "slotStartTime": "10:30 AM",
    "slotEndTime": "10:45 AM"
}, {
    "slotStartTime": "10:45 AM",
    "slotEndTime": "11:00 AM"
}, {
    "slotStartTime": "11:00 AM",
    "slotEndTime": "11:15 AM"
}, {
    "slotStartTime": "11:15 AM",
    "slotEndTime": "11:30 AM"
}, {
    "slotStartTime": "11:30 AM",
    "slotEndTime": "11:45 AM"
}, {
    "slotStartTime": "11:45 AM",
    "slotEndTime": "12:00 PM"
}, {
    "slotStartTime": "12:00 PM",
    "slotEndTime": "12:15 PM"
}, {
    "slotStartTime": "12:15 PM",
    "slotEndTime": "12:30 PM"
}, {
    "slotStartTime": "12:30 PM",
    "slotEndTime": "12:45 PM"
}, {
    "slotStartTime": "12:45 PM",
    "slotEndTime": "01:00 PM"
}, {
    "slotStartTime": "01:00 PM",
    "slotEndTime": "01:15 PM"
}, {
    "slotStartTime": "01:15 PM",
    "slotEndTime": "01:30 PM"
}, {
    "slotStartTime": "01:30 PM",
    "slotEndTime": "01:45 PM"
}, {
    "slotStartTime": "01:45 PM",
    "slotEndTime": "02:00 PM"
}, {
    "slotStartTime": "02:00 PM",
    "slotEndTime": "02:15 PM"
}, {
    "slotStartTime": "02:15 PM",
    "slotEndTime": "02:30 PM"
}, {
    "slotStartTime": "02:30 PM",
    "slotEndTime": "02:45 PM"
}, {
    "slotStartTime": "02:45 PM",
    "slotEndTime": "03:00 PM"
}, {
    "slotStartTime": "03:00 PM",
    "slotEndTime": "03:15 PM"
}, {
    "slotStartTime": "03:15 PM",
    "slotEndTime": "03:30 PM"
}, {
    "slotStartTime": "03:30 PM",
    "slotEndTime": "03:45 PM"
}, {
    "slotStartTime": "03:45 PM",
    "slotEndTime": "04:00 PM"
}, {
    "slotStartTime": "04:00 PM",
    "slotEndTime": "04:15 PM"
}, {
    "slotStartTime": "04:15 PM",
    "slotEndTime": "04:30 PM"
}, {
    "slotStartTime": "04:30 PM",
    "slotEndTime": "04:45 PM"
}, {
    "slotStartTime": "04:45 PM",
    "slotEndTime": "05:00 PM"
}, {
    "slotStartTime": "05:00 PM",
    "slotEndTime": "05:15 PM"
}, {
    "slotStartTime": "05:15 PM",
    "slotEndTime": "05:30 PM"
}, {
    "slotStartTime": "05:30 PM",
    "slotEndTime": "05:45 PM"
}, {
    "slotStartTime": "05:45 PM",
    "slotEndTime": "06:00 PM"
}, {
    "slotStartTime": "06:00 PM",
    "slotEndTime": "06:15 PM"
}, {
    "slotStartTime": "06:15 PM",
    "slotEndTime": "06:30 PM"
}, {
    "slotStartTime": "06:30 PM",
    "slotEndTime": "06:45 PM"
}, {
    "slotStartTime": "06:45 PM",
    "slotEndTime": "07:00 PM"
}, {
    "slotStartTime": "07:00 PM",
    "slotEndTime": "07:15 PM"
}, {
    "slotStartTime": "07:15 PM",
    "slotEndTime": "07:30 PM"
}, {
    "slotStartTime": "07:30 PM",
    "slotEndTime": "07:45 PM"
}, {
    "slotStartTime": "07:45 PM",
    "slotEndTime": "08:00 PM"
}, {
    "slotStartTime": "08:00 PM",
    "slotEndTime": "08:15 PM"
}, {
    "slotStartTime": "08:15 PM",
    "slotEndTime": "08:30 PM"
}, {
    "slotStartTime": "08:30 PM",
    "slotEndTime": "08:45 PM"
}, {
    "slotStartTime": "08:45 PM",
    "slotEndTime": "09:00 PM"
}, {
    "slotStartTime": "09:00 PM",
    "slotEndTime": "09:15 PM"
}, {
    "slotStartTime": "09:15 PM",
    "slotEndTime": "09:30 PM"
}, {
    "slotStartTime": "09:30 PM",
    "slotEndTime": "09:45 PM"
}, {
    "slotStartTime": "09:45 PM",
    "slotEndTime": "10:00 PM"
}, {
    "slotStartTime": "10:00 PM",
    "slotEndTime": "10:15 PM"
}, {
    "slotStartTime": "10:15 PM",
    "slotEndTime": "10:30 PM"
}, {
    "slotStartTime": "10:30 PM",
    "slotEndTime": "10:45 PM"
}, {
    "slotStartTime": "10:45 PM",
    "slotEndTime": "11:00 PM"
}, {
    "slotStartTime": "11:00 PM",
    "slotEndTime": "11:15 PM"
}, {
    "slotStartTime": "11:15 PM",
    "slotEndTime": "11:30 PM"
}, {
    "slotStartTime": "11:30 PM",
    "slotEndTime": "11:45 PM"
}, {
    "slotStartTime": "11:45 PM",
    "slotEndTime": "12:00 AM"
}])