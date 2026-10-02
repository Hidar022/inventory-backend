from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination


class SalesPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_page_number(self, request, paginator):
        page_number = request.query_params.get(self.page_query_param, "1")
        if page_number.lower() in self.last_page_strings:
            return page_number
        if not page_number.isdigit() or int(page_number) < 1:
            raise ValidationError({"page": "Enter a positive page number."})
        return page_number

    def get_page_size(self, request):
        page_size = request.query_params.get(self.page_size_query_param)
        if page_size is None:
            return self.page_size
        try:
            page_size = int(page_size)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"page_size": "Enter a positive page size."}) from exc
        if page_size < 1:
            raise ValidationError({"page_size": "Enter a positive page size."})
        return min(page_size, self.max_page_size)