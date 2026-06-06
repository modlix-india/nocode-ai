FUNCTION on_load
    LOGIC
        setStore: UIEngine.SetStore(path = "Page.firstTableData", value = [{
    "name": "Total Leasable Area (Sqft)",
    "value": "70,447"
}, {
    "name": "Tenant",
    "value": "EMIDS Technologies Pvt, Ltd"
}, {
    "name": "Lease start date",
    "value": "Feb-20"
}, {
    "name": "Lease expiry date",
    "value": "Jan-28"
}, {
    "name": "Lock In period",
    "value": "8 Years with 3 month notice period"
}, {
    "name": "Lease period",
    "value": "9 Years"
}, {
    "name": "Rent escalation",
    "value": "5% YOY"
}, {
    "name": "Security deposit",
    "value": "INR 6 Crs"
}])
        setStore_Copy_1_Copy_1: UIEngine.SetStore(path = "Page.secondTableData", value = [{
    "name": "Base rent",
    "value": "INR 85.00 PSF"
}, {
    "name": "Car parking",
    "value": "INR 2,000 per slot"
}, {
    "name": "Cafeteria rent",
    "value": "INR 3,40,000"
}])
        setStore_Copy_1: UIEngine.SetStore(path = "Page.thirdTableData", value = [{
    "name": "Base rent",
    "value": "INR 103.32 PSF"
}, {
    "name": "Car parking",
    "value": "INR 2,431 per slot"
}, {
    "name": "Cafeteria rent",
    "value": "INR 4,13,272"
}])
        setStore1: UIEngine.SetStore(path = "Page.activeButton", value = "overview")