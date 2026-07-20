FUNCTION showEnterDetailsGrid
    LOGIC
        setStore2: UIEngine.SetStore(path = "Page.calenderGrid", value = false)
        setStore_3: UIEngine.SetStore(path = "Page.showEnterDetails", value = true)
        setStore_Copy_1: UIEngine.SetStore(path = "Page.moveForward", value = {{Page.moveForward}} + 1)
        setStore1: UIEngine.SetStore(path = "Page.scheduleCallDetails.bookingTime", value = Page.slotsArray[Parent.__index])