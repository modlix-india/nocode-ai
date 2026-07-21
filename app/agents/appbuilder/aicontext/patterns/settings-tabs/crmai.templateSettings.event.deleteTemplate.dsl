FUNCTION deleteTemplate
    LOGIC
        setStore1: UIEngine.SetStore(path = "Page.templateName", value = Parent.name)
            output
                deleteRequest: CoreServices.REST.DeleteRequest(headers = {
    "Authorization": "Bearer EAAcZAg7yuLnMBO4AzTdaKoFdUbkzbaj6IZBMlmvrWc6SS6gWNJbR7gG9M08Jgov1Xf0IeI5WUtuhL21qNE7tgyE4sYWxzbTLesUZAZAvuG5FWlH7FZChBsIvYfh1CbiOfiHzD7WgJmZAg1H47TM6AbkNc4CKLZACvYvMRGb0tjA6ZAkuDRO7uf6rPEoh"
}, url = `'{{Page.phoneNumberId}}/message_templates?hsm_id={{Page.activeTemplateId}}&name={{Page.templateName}}'`, connectionName = "metaConnection") AFTER Steps.setStore1.output
                    error
                        setStore2: UIEngine.SetStore(path = "Page.deleteTemplateError", value = Steps.deleteRequest.error.data)
                    output
                        getAllTemplatesnew: _.getAllTemplatesnew() AFTER Steps.deleteRequest.output