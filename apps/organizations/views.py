from rest_framework import generics, permissions
from rest_framework.exceptions import PermissionDenied

from apps.organizations.models import Organization
from apps.organizations.permissions import (
    IsOrganizationMember,
    IsOwner,
    OrganizationContextMixin,
)
from apps.organizations.serializers import OrganizationCreateSerializer, OrganizationSerializer


class OrganizationCreateView(OrganizationContextMixin, generics.CreateAPIView):
    queryset = Organization.objects.all()
    serializer_class = OrganizationCreateSerializer
    permission_classes = [permissions.IsAuthenticated]


class CurrentOrganizationView(OrganizationContextMixin, generics.RetrieveUpdateAPIView):
    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    permission_classes = [permissions.IsAuthenticated, IsOrganizationMember]

    def get_permissions(self):
        permissions_list = [permissions.IsAuthenticated, IsOrganizationMember]
        if self.request.method in {"PATCH", "PUT", "DELETE"}:
            permissions_list.append(IsOwner)
        return [permission() for permission in permissions_list]

    def get_object(self):
        membership = getattr(self.request, "membership", None)
        if not membership or not membership.is_active:
            raise PermissionDenied("You do not have access to an active organization.")
        return membership.organization
