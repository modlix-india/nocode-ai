FUNCTION payment
    NAMESPACE cxapp
    PARAMETERS
        customerEmail AS {}
        customerName AS {}
        linkId AS {"type": "STRING", "version": 1, "maxLength": 49}
        amount AS {"type": "DOUBLE", "version": 1}
        purpose AS {"defaultValue": "Payment", "version": 1, "type": "STRING", "maxLength": 499}
        returnUrl AS {"type": "STRING", "version": 1}
        UpiIntent AS {"type": "BOOLEAN", "version": 1}
        paymentMethods AS {"type": "STRING", "version": 1, "enums": ["cc", "dc", "ccc", "ppc", "nb", "upi", "paypal", "app"], "defaultValue": "upi"}
        customerPhoneNumber AS {"type": "STRING", "version": 1}
        sendEmail AS {"type": "BOOLEAN", "version": 1}
        sendSms AS {"type": "BOOLEAN", "version": 1}
        requestId AS {"type": "STRING", "version": 1}
        idempotency AS {"type": "STRING", "version": 1}
        notify_url AS {"type": "STRING", "version": 1}
        id AS {"type": "STRING", "version": 1}
        code AS {"type": "STRING", "version": 1}
        expiryTime AS {"type": "STRING", "version": 1}
    EVENTS
        output
            data AS {"type": "OBJECT", "version": 1}
        error
            message AS {"type": "OBJECT", "version": 1}
    LOGIC
        create: System.Context.Create(schema = {
    "type": "OBJECT"
}, name = "details")
            output
                smsNotify: System.Context.Set(value = Arguments.sendSms, name = "Context.details.link_notify.send_sms") AFTER Steps.create.output
                currenyType: System.Context.Set(name = "Context.details.link_currency", value = "INR") AFTER Steps.create.output
                EmailNotify: System.Context.Set(name = "Context.details.link_notify.send_email", value = Arguments.sendEmail) AFTER Steps.create.output
                customerEmail: System.Context.Set(value = Arguments.customerEmail, name = "Context.details.customer_details.customer_email") AFTER Steps.create.output
                customerName: System.Context.Set(name = "Context.details.customer_details.customer_name", value = Arguments.customerName) AFTER Steps.create.output
                customerPhoneNumber: System.Context.Set(value = Arguments.customerPhoneNumber, name = "Context.details.customer_details.customer_phone") AFTER Steps.create.output
                set3: System.Context.Set(name = "Context.details.link_amount", value = Arguments.amount) AFTER Steps.create.output
                linkId: System.Context.Set(name = "Context.details.link_id", value = Arguments.linkId) AFTER Steps.create.output
                set4: System.Context.Set(name = "Context.details.notify_url", value = Arguments.notify_url) AFTER Steps.create.output
                returnUrl: System.Context.Set(name = "Context.details.link_meta.return_url", value = Arguments.returnUrl) AFTER Steps.create.output
                set: System.Context.Set(name = "Context.details.link_meta.upi_intent", value = Arguments.UpiIntent) AFTER Steps.create.output
                set1: System.Context.Set(name = "Context.details.link_purpose", value = Arguments.purpose) AFTER Steps.create.output
        create1: System.Context.Create(name = "headers", schema = {
    "type": "OBJECT"
})
            output
                make: System.Make(resultShape = {
    "content-type": "application/json",
    "x-api-version": "<PHONE>",
    "x-client-id": "{{Arguments.id}}",
    "x-client-secret": "<REDACTED>"
}) AFTER Steps.create1.output
                    output
                        set5: System.Context.Set(name = "Context.headers", value = Steps.make.output.value)
                            output
                                postRequest: CoreServices.REST.PostRequest(url = "/pg/links", headers = Context.headers, connectionName = "CashFreePayments", payload = Context.payLoad) AFTER Steps.customerEmail.output, Steps.customerName.output, Steps.customerPhoneNumber.output, Steps.smsNotify.output, Steps.EmailNotify.output, Steps.returnUrl.output, Steps.set.output, Steps.currenyType.output, Steps.linkId.output, Steps.set1.output, Steps.set3.output, Steps.set4.output, Steps.set5.output, Steps.set6.output
                                    output
                                        objectConvert: System.Object.ObjectConvert(source = Steps.postRequest.output.data, schema = {
    "ref": "cxapp.cashFree.PaymentDetails"
}, conversionMode = "USE_DEFAULT")
                                            output
                                                update: CoreServices.Storage.Update(storageName = "cashFreePaymentDetails", appCode = "cxapp", dataObject = Steps.objectConvert.output.value, dataObjectId = Arguments.linkId, isPartial = true)
                                                    output
                                                        generateEvent1: System.GenerateEvent(results = {
    "name": "data",
    "value": {
        "isExpression": true,
        "value": "Steps.update.output.result"
    }
})
        create1_Copy_1: System.Context.Create(name = "payLoad", schema = {
    "type": "OBJECT"
})
            output
                make1: System.Make(resultShape = {
    "customer_details": {
        "customer_email": "{{Arguments.customerEmail}}",
        "customer_name": "{{Arguments.customerName}}",
        "customer_phone": "{{Arguments.customerPhoneNumber}}"
    },
    "link_amount": "{{Arguments.amount}}",
    "link_auto_reminders": "true",
    "link_currency": "INR",
    "link_expiry_time": "{{Arguments.expiryTime}}",
    "link_id": "{{Arguments.linkId}}",
    "link_meta": {
        "notify_url": "{{Arguments.notify_url}}",
        "return_url": "{{Arguments.returnUrl}}",
        "upi_intent": "{{Arguments.UpiIntent}}"
    },
    "link_purpose": "{{Arguments.purpose}}"
}) AFTER Steps.create1_Copy_1.output
                    output
                        set6: System.Context.Set(name = "Context.payLoad", value = Steps.make1.output.value)